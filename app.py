import os
import io
import re
import base64
import json
import logging
from typing import Optional

import aiohttp
from PIL import Image  # для конвертации в PNG
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

IMGBB_MAX_BYTES = 32 * 1024 * 1024  # лимит imgbb ~32MB

# ---------- УТИЛИТЫ ----------
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")  # допустимые ID: буквы/цифры/_/-

def sanitize_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    if ID_RE.match(s):
        return s
    return None

def bytes_to_png(original_bytes: bytes) -> bytes:
    \"\"\"Конвертирует любые поддерживаемые форматы (jpg, webp, heic* и т.п.) в PNG.\"\"\"
    with Image.open(io.BytesIO(original_bytes)) as im:
        # Приводим к режиму, поддерживаемому PNG
        if im.mode not in ("RGB", "RGBA", "P", "L"):
            im = im.convert("RGBA")
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=False)
        return out.getvalue()

async def upload_to_imgbb(image_bytes: bytes, name: str) -> dict:
    \"\"\"Загружает PNG-байты в imgbb с именем name (например, 'UZ001450.png').\"\"\"
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    url = "https://api.imgbb.com/1/upload"
    data = {"key": IMGBB_API_KEY, "image": b64_str, "name": name}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, timeout=180) as resp:
            txt = await resp.text()
            try:
                payload = json.loads(txt)
            except json.JSONDecodeError:
                log.error("imgbb: неверный JSON ответ: %s", txt)
                raise RuntimeError("imgbb вернул неверный ответ")

            if resp.status != 200 or not payload.get("success"):
                err = payload.get("error", {})
                message = err.get("message") or f"HTTP {resp.status}"
                log.error("imgbb: ошибка загрузки: %s | ответ: %s", message, txt)
                raise RuntimeError(f"Ошибка imgbb: {message}")

            d = payload["data"]
            return {
                "url": d.get("url"),
                "display_url": d.get("display_url"),
                "delete_url": d.get("delete_url"),
                "size": d.get("size"),
                "id": d.get("id"),
            }

# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь картинку, затем введи ID (например UZ001450).\\n"
        "Я загружу её в imgbb как UZ001450.png и пришлю прямую ссылку.\\n\\n"
        "/help — подробности, /cancel — отмена ожидания ID."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Сценарий:\\n"
        "1) Отправь изображение (фото или документ с image/*)\\n"
        "2) Я попрошу ввести ID (пример: UZ001450)\\n"
        "3) Пришлю прямую ссылку вида .../UZ001450.png\\n\\n"
        "Команды: /cancel — отменить ожидание кода"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_image", None)
    await update.message.reply_text("Ок, отменил. Отправь новую картинку, если хочешь начать заново.")

# ---------- ОБРАБОТЧИКИ ----------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    \"\"\"1) Принимаем картинку, сохраняем байты, 2) просим ввести ID.\"\"\"
    message = update.message

    # Получаем байты из фото или image-документа
    image_bytes = None
    if message.photo:
        photo = message.photo[-1]
        file = await photo.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        image_bytes = buf.getvalue()
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        doc = message.document
        file = await doc.get_file()
        buf = io.BytesIO()
        await file.download_to_memory(out=buf)
        image_bytes = buf.getvalue()
    else:
        return  # не изображение

    if len(image_bytes) > IMGBB_MAX_BYTES:
        await message.reply_text("❌ Файл больше 32 МБ. Сожми изображение и попробуй снова.")
        return

    # Сохраняем исходные байты и просим ID
    context.user_data["pending_image"] = image_bytes
    await message.reply_text(
        "Картинка получена ✅\\nТеперь отправь ID (например: UZ001450). "
        "Отправь ТОЛЬКО сам код без лишнего текста.\\n/cancel — отмена"
    )

async def handle_text_as_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    \"\"\"Если есть pending_image, трактуем текст как ID -> конвертируем в PNG -> заливаем.\"\"\"
    message = update.message
    pending = context.user_data.get("pending_image")
    if pending is None:
        return  # обычный текст

    the_id = sanitize_id(message.text or "")
    if not the_id:
        await message.reply_text(
            "❌ Некорректный ID. Разрешены буквы/цифры/подчёркивание/дефис, 2–64 символа.\\n"
            "Попробуй ещё раз. Пример: UZ001450\\n/cancel — отмена"
        )
        return

    # Конвертируем в PNG
    try:
        png_bytes = bytes_to_png(pending)
    except Exception as e:
        log.exception("Ошибка конвертации в PNG")
        await message.reply_text(f"❌ Не удалось конвертировать изображение в PNG: {e}")
        context.user_data.pop("pending_image", None)
        return

    if len(png_bytes) > IMGBB_MAX_BYTES:
        await message.reply_text("❌ После конвертации PNG получилось больше 32 МБ. Сожми изображение и попробуй снова.")
        context.user_data.pop("pending_image", None)
        return

    # Заголовок файла на imgbb: {ID}.png
    name = f"{the_id}.png"

    # Загружаем
    try:
        info = await upload_to_imgbb(png_bytes, name=name)
    except Exception as e:
        log.exception("Ошибка загрузки в imgbb")
        await message.reply_text(f"❌ Не удалось загрузить в imgbb: {e}")
        context.user_data.pop("pending_image", None)
        return

    context.user_data.pop("pending_image", None)

    # Прямая ссылка
    url = info.get("url")
    display_url = info.get("display_url")
    delete_url = info.get("delete_url")
    size = info.get("size")

    lines = ["✅ Готово!"]
    if url:
        lines.append(f"Прямая ссылка: {url}")  # должна оканчиваться на /{ID}.png
    if display_url:
        lines.append(f"Страница: {display_url}")
    if size:
        lines.append(f"Размер: {size} байт")
    lines.append(f"Файл: {name}")
    if delete_url:
        lines.append(f"(Удаление: {delete_url})")

    await message.reply_text("\\n".join(lines))

# ---------- СБОРКА ----------
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))

    # 1) Принимаем изображения
    app.add_handler(MessageHandler(filters.PHOTO | (filters.Document.IMAGE), handle_image))

    # 2) Если есть \"pending_image\", следующий текст — это ID
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_as_id))

    return app

def main():
    app = build_application()
    if WEBHOOK_URL:
        log.info("Старт в режиме webhook на порту %s", PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"/{BOT_TOKEN}",
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}",
        )
    else:
        log.info("Старт в режиме polling")
        app.run_polling(allowed_updates=["message"])

if __name__ == "__main__":
    main()
