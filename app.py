import os
import io
import re
import base64
import json
import logging
import asyncio
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
    log.warning("IMGBB_API_KEY не задан — основной провайдер работать не будет")

# ---------- НАСТРОЙКИ ----------
IMGBB_MAX_BYTES = 32 * 1024 * 1024
TELEGRAPH_MAX_BYTES = int(4.7 * 1024 * 1024)  # лимит telegra.ph ~5 МБ
MAX_SIDE_PX = 1600
JPEG_QUALITY = 75
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

# ---------- УТИЛИТЫ ----------
def sanitize_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    return s if ID_RE.match(s) else None

def to_clean_jpeg(original_bytes: bytes, max_side: int = 1000, quality: int = 80) -> bytes:
    """Превращает любую картинку в «чистый» JPEG (RGB, без EXIF)."""
    im = Image.open(io.BytesIO(original_bytes))
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        if w >= h:
            new_w = max_side
            new_h = int(h * (max_side / w))
        else:
            new_h = max_side
            new_w = int(w * (max_side / h))
        im = im.resize((new_w, new_h), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

def encode_jpeg(original_bytes: bytes, max_side: int = MAX_SIDE_PX, quality: int = JPEG_QUALITY) -> bytes:
    im = Image.open(io.BytesIO(original_bytes))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > max_side:
        if w >= h:
            new_w = max_side
            new_h = int(h * (max_side / w))
        else:
            new_h = max_side
            new_w = int(w * (max_side / h))
        im = im.resize((new_w, new_h), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

# ---------- IMGBB ----------
async def upload_to_imgbb(image_bytes: bytes, name: str) -> dict:
    if not IMGBB_API_KEY:
        raise RuntimeError("IMGBB_API_KEY не задан")
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    url = "https://api.imgbb.com/1/upload"
    data = {"key": IMGBB_API_KEY, "image": b64_str, "name": name}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, timeout=90) as resp:
            txt = await resp.text()
            log.info("imgbb response (status %s): %s", resp.status, txt[:200])
            payload = json.loads(txt)
            if resp.status != 200 or not payload.get("success"):
                raise RuntimeError(f"Ошибка imgbb: {payload}")
            return payload["data"]

# ---------- TELEGRAPH ----------
async def upload_to_telegraph(image_bytes: bytes) -> str:
    url = "https://telegra.ph/upload"
    form = aiohttp.FormData()
    form.add_field("file", image_bytes, filename="file.jpg", content_type="application/octet-stream")
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form, timeout=30) as resp:
            txt = await resp.text()
            log.info("telegraph response (status %s): %s", resp.status, txt[:200])
            try:
                payload = json.loads(txt)
            except:
                raise RuntimeError(f"Telegraph не JSON: {txt}")
            if resp.status != 200 or not isinstance(payload, list):
                raise RuntimeError(f"Telegraph HTTP {resp.status}: {payload}")
            src = payload[0]["src"]
            return f"https://telegra.ph{src}"

# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь картинку, потом введи ID (например UZ001450).\n"
        "Сначала попробую загрузить в ImgBB, если не получится — в Telegraph.\n"
        "/cancel — отменить."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_image", None)
    await update.message.reply_text("Ожидание кода отменено.")

# ---------- ОБРАБОТЧИКИ ----------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    tg_file = None
    if message.photo:
        tg_file = await message.photo[-1].get_file()
    elif message.document and message.document.mime_type.startswith("image/"):
        tg_file = await message.document.get_file()
    if not tg_file:
        return
    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    context.user_data["pending_image"] = buf.getvalue()
    await message.reply_text("Картинка получена ✅. Теперь введи ID.")

async def handle_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_image")
    if not pending:
        return
    the_id = sanitize_id(update.message.text)
    if not the_id:
        await update.message.reply_text("❌ Некорректный ID")
        return
    await update.message.reply_text("Загружаю…")

    # 1) Пробуем IMGBB
    try:
        jpeg_bytes = encode_jpeg(pending)
        data = await upload_to_imgbb(jpeg_bytes, name=f"{the_id}.jpg")
        context.user_data.pop("pending_image", None)
        url = data.get("url")
        await update.message.reply_text(f"✅ ImgBB\n{url}\nФайл: {the_id}.jpg")
        return
    except Exception as e:
        log.warning("IMGBB ошибка: %s", e)

    # 2) Пробуем Telegraph (жёсткий JPEG)
    try:
        clean_jpeg = to_clean_jpeg(pending, max_side=1000, quality=80)
        if len(clean_jpeg) > TELEGRAPH_MAX_BYTES:
            raise RuntimeError("Слишком большой файл для Telegraph")
        t_url = await upload_to_telegraph(clean_jpeg)
        context.user_data.pop("pending_image", None)
        await update.message.reply_text(f"✅ Telegraph\n{t_url}?code={the_id}")
        return
    except Exception as e:
        context.user_data.pop("pending_image", None)
        await update.message.reply_text(f"❌ Не удалось загрузить ни в ImgBB, ни в Telegraph: {e}")

# ---------- MAIN ----------
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
        log.info("Webhook mode")
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=f"/{BOT_TOKEN}",
                        webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}")
    else:
        log.info("Polling mode")
        app.run_polling()

if __name__ == "__main__":
    main()
