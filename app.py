import os
import io
import re
import base64
import json
import logging
from typing import Optional

import aiohttp
from PIL import Image
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("imgbb-bot")

# ---------- ОКРУЖЕНИЕ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN")
if not IMGBB_API_KEY:
    raise RuntimeError("Не задана переменная окружения IMGBB_API_KEY")

IMGBB_MAX_BYTES = 32 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

# ---------- УТИЛИТЫ ----------
def sanitize_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    return s if ID_RE.match(s) else None

def bytes_to_png(original_bytes: bytes) -> bytes:
    """Конвертирует любые поддерживаемые форматы (jpg, webp и т.п.) в PNG."""
    with Image.open(io.BytesIO(original_bytes)) as im:
        if im.mode not in ("RGB", "RGBA", "P", "L"):
            im = im.convert("RGBA")
        out = io.BytesIO()
        im.save(out, format="PNG")
        return out.getvalue()

async def upload_to_imgbb(image_bytes: bytes, name: str) -> dict:
    """Загружает PNG-байты в imgbb с именем name (например, 'UZ001450.png')."""
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    url = "https://api.imgbb.com/1/upload"
    data = {"key": IMGBB_API_KEY, "image": b64_str, "name": name}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, timeout=180) as resp:
            txt = await resp.text()
            payload = json.loads(txt)
            if resp.status != 200 or not payload.get("success"):
                raise RuntimeError(f"Ошибка imgbb: {txt}")
            return payload["data"]

# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь картинку, потом введи ID (например UZ001450). "
        "Я загружу её в imgbb как UZ001450.png и пришлю прямую ссылку.\n\n"
        "/cancel — отменить ожидание кода."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_image", None)
    await update.message.reply_text("Ожидание кода отменено.")

# ---------- ОБРАБОТЧИКИ ----------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимаем картинку → сохраняем байты → просим ввести ID."""
    message = update.message
    file = None

    if message.photo:
        file = await message.photo[-1].get_file()
    elif message.document and message.document.mime_type.startswith("image/"):
        file = await message.document.get_file()

    if not file:
        return

    buf = io.BytesIO()
    await file.download_to_memory(out=buf)
    image_bytes = buf.getvalue()

    if len(image_bytes) > IMGBB_MAX_BYTES:
        await message.reply_text("❌ Файл больше 32 МБ. Сожми изображение и попробуй снова.")
        return

    context.user_data["pending_image"] = image_bytes
    await message.reply_text("Картинка получена ✅. Теперь введи ID (например UZ001450).")

async def handle_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если есть pending_image, трактуем текст как ID → конвертируем в PNG → загружаем."""
    pending = context.user_data.get("pending_image")
    if not pending:
        return

    the_id = sanitize_id(update.message.text)
    if not the_id:
        await update.message.reply_text("❌ Некорректный ID. Разрешены буквы/цифры/`_`/`-`.")
        return

    png_bytes = bytes_to_png(pending)
    name = f"{the_id}.png"

    try:
        data = await upload_to_imgbb(png_bytes, name=name)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        context.user_data.pop("pending_image", None)
        return

    context.user_data.pop("pending_image", None)
    url = data.get("url")
    await update.message.reply_text(f"✅ Загружено!\nПрямая ссылка: {url}")

# ---------- СБОРКА ----------
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_id))
    return app

def main():
    app = build_app()
    if WEBHOOK_URL:
        log.info("Старт в режиме webhook на порту %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"/{BOT_TOKEN}",
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        )
    else:
        log.info("Старт в режиме polling")
        app.run_polling()

if __name__ == "__main__":
    main()
