# Telegram → imgbb uploader (ID → filename)

Бот принимает изображение, просит ввести **ID**, конвертирует в **PNG** и загружает в **imgbb** с именем **`{ID}.png`**. В ответ даёт **прямую ссылку** вида `https://i.ibb.co/.../UZ001450.png`.

## Быстрый старт (локально — polling)
```bash
pip install -r requirements.txt
# Windows PowerShell:
setx BOT_TOKEN "<твой токен>"
setx IMGBB_API_KEY "<твой ключ>"
# Linux/macOS (на время сессии):
export BOT_TOKEN="<твой токен>"
export IMGBB_API_KEY="<твой ключ>"

python app.py
```

## Railway (webhook)
1. Залей репозиторий на GitHub.
2. На Railway: New Project → Deploy from GitHub Repo.
3. В Variables добавь:
   - `BOT_TOKEN`
   - `IMGBB_API_KEY`
   - `WEBHOOK_URL` = `https://<project>.up.railway.app` (домен проекта из Overview → Domains)
4. Дождись статуса **Running**. Бот начнёт принимать апдейты по вебхуку.

## Скрипт
Основной код — в `app.py` (python-telegram-bot v20.7, aiohttp, Pillow).

## Команды бота
- `/start` — приветствие
- `/help` — подсказка по шагам
- `/cancel` — отмена ожидания ID

## Лицензия
MIT
