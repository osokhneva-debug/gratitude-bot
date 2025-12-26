import os
from dotenv import load_dotenv

load_dotenv()

# Токен бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен! Добавь его в переменные окружения.")

# Путь к базе данных
DATABASE_PATH = os.getenv("DATABASE_PATH", "gratitude.db")
