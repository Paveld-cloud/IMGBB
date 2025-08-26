import os
import io
import re
import base64
import json
import logging
import asyncio
from typing import Optional, Tuple

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

# лимиты
IMGBB_MAX_BYTES = 32 * 1024 * 1024
MAX_SIDE_PX = 4096  # даунскейл больших изображений

ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

# ---------- УТИЛИТЫ ----------
def sanitize_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    return s if ID_RE.match(s) else None

def _open_image(original_bytes: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(original_bytes))
    try:
        im.load()
    except Exception:
        pass
    return im

def _downscale(im: Image.Image, max_side: int = MAX_SIDE_PX) -> Image.Image:
    w, h = im.size
    if max(w, h) <= max_side:
        return im
    if w >= h:
        new_w = max_side
        new_h = int(h * (max_side / w))
    else:
        new_h = max_side
        new_w = int(w * (max_side / h))
    return im.resize((new_w, new_h), Image.LANCZOS)

def encode_png(original_bytes: bytes, max_side: int = MAX_SIDE_PX) -> bytes:
    """Конвертирует в PNG, даунскейлит до max_side."""
    im = _open_image(original_bytes)
    im = _downscale(im, max_side)
    if im.mode not in ("RGB", "RGBA", "P", "L"):
        im = im.convert("RGBA")
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()

def encode_jpeg(original_bytes: bytes, max_side: int = MAX_SIDE_PX, quality: int = 85) -> bytes:
    """Конвертирует в JPEG с даунскейлом и умеренным качеством (для облегчения)."""
    im = _open_image(original_bytes)
    im = _downscale(im, max_side)
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

async def upload_to_imgbb(image_bytes: bytes, name: str, timeout_s: int = 300) -> dict:
    """Одна попытка загрузки в imgbb. Бросает исключение при неуспехе."""
    b64_str = base64.b64encode(image_bytes).decode("utf-8")
    url = "https://api.imgbb.com/1/upload"
    data = {"key": IMGBB_API_KEY, "image": b64_str, "name": name}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=data, timeout=timeout_s) as resp:
            txt = await resp.text()
            log.info("imgbb response (status %s): %s", resp.status, txt[:500])

            # Пытаемся распарсить JSON
            try:
                payload = json.loads(txt)
            except json.JSONDecodeError:
                raise RuntimeError(f"imgbb вернул не-JSON. Ответ: {txt[:200]}")

            if resp.status != 200 or not payload.get("success"):
                # Ошибки imgbb приходят как {"success":false,"error":{...}}
                err = payload.get("error") or {}
                raise RuntimeError(f"Ошибка imgbb: {err or payload}")

            return payload["data"]

async def upload_with_retries(image_bytes: bytes, name: str, max_attempts: int = 3) -> dict:
    """Повторная загрузка с экспоненциальной паузой при 5xx/сетевых ошибках/429."""
    attempt = 0
    last_exc = None
    while attempt < max_attempts:
        attempt += 1
        try:
            log.info("Загрузка в imgbb, попытка %d/%d (%s)", attempt, max_attempts, name)
            return await upload_to_imgbb(image_bytes, name, timeout_s=300)
        except Exception as e:
            last_exc = e
            msg = str(e)
            # Решаем — стоит ли ретраить
            retryable = any(code in msg for code in ["504", "503", "502", "500", "429"]) or "не-JSON" in msg or "Client" in msg
            if attempt >= max_attempts or not retryable:
                break
            sleep_s = 2 * attempt  # 2, 4, 6
            log.warning("Ошибка загрузки (попытка %d): %s. Повтор через %ss", attempt, e, sleep_s)
            await asyncio.sleep(sleep_s)
    raise RuntimeError(f"Не удалось загрузить в imgbb после {max_attempts} попыток: {last_exc}")

# ---------- КОМАНДЫ ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Отправь картинку, затем введи ID (например UZ001450). "
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
    tg_file = None

    if message.photo:
        tg_file = await message.photo[-1].get_file()
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        tg_file = await message.document.get_file()

    if not tg_file:
        return

    buf = io.BytesIO()
    await tg_file.download_to_memory(out=buf)
    image_bytes = buf.getvalue()

    if len(image_bytes) > IMGBB_MAX_BYTES:
        await message.reply_text("❌ Файл больше 32 МБ. Сожми изображение и попробуй снова.")
        return

    context.user_data["pending_image"] = image_bytes
    await message.reply_text("Картинка получена ✅. Теперь введи ID (например UZ001450).")

async def handle_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Если есть pending_image, трактуем текст как ID → PNG попытка → при неудаче JPEG попытка."""
    pending = context.user_data.get("pending_image")
    if not pending:
        return

    the_id = sanitize_id(update.message.text)
    if not the_id:
        await update.message.reply_text("❌ Некорректный ID. Разрешены буквы/цифры/`_`/`-` (2–64 символа).")
        return

    # --- Готовим PNG (даунскейл до 4096px) ---
    try:
        png_bytes = encode_png(pending, max_side=MAX_SIDE_PX)
    except Exception as e:
        log.exception("Ошибка конвертации в PNG")
        await update.message.reply_text(f"❌ Не удалось конвертировать в PNG: {e}")
        context.user_data.pop("pending_image", None)
        return

    if len(png_bytes) > IMGBB_MAX_BYTES:
        await update.message.reply_text("❌ После конвертации PNG > 32 МБ. Попробуем JPEG автоматически.")
        png_bytes = None  # не будем пытаться PNG

    # --- Сначала пробуем PNG с ретраями ---
    if png_bytes:
        try:
            data = await upload_with_retries(png_bytes, name=f"{the_id}.png", max_attempts=3)
            context.user_data.pop("pending_image", None)
            url = data.get("url")
            size = data.get("size")
            await update.message.reply_text(
                (f"✅ Загружено!\nПрямая ссылка: {url}\nФайл: {the_id}.png\nРазмер: {size} байт")
                if size else (f"✅ Загружено!\nПрямая ссылка: {url}\nФайл: {the_id}.png")
            )
            return
        except Exception as e:
            log.warning("PNG не удался, попробуем JPEG. Причина: %s", e)

    # --- Фоллбэк: пробуем JPEG (обычно меньше) ---
    try:
        jpeg_bytes = encode_jpeg(pending, max_side=MAX_SIDE_PX, quality=85)
    except Exception as e:
        log.exception("Ошибка конвертации в JPEG")
        await update.message.reply_text(f"❌ Не удалось конвертировать в JPEG: {e}")
        context.user_data.pop("pending_image", None)
        return

    if len(jpeg_bytes) > IMGBB_MAX_BYTES:
        await update.message.reply_text("❌ Даже JPEG > 32 МБ после сжатия. Сожми изображение и попробуй снова.")
        context.user_data.pop("pending_image", None)
        return

    try:
        data = await upload_with_retries(jpeg_bytes, name=f"{the_id}.jpg", max_attempts=3)
    except Exception as e:
        await update.message.reply_text(f"❌ Загрузка в imgbb не удалась (даже JPEG): {e}")
        context.user_data.pop("pending_image", None)
        return

    context.user_data.pop("pending_image", None)
    url = data.get("url")
    size = data.get("size")
    await update.message.reply_text(
        (f"✅ Загружено!\nПрямая ссылка: {url}\nФайл: {the_id}.jpg\nРазмер: {size} байт")
        if size else (f"✅ Загружено!\nПрямая ссылка: {url}\nФайл: {the_id}.jpg")
    )

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
