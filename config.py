import os
from dotenv import load_dotenv

load_dotenv()

# Токен бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен! Добавь его в переменные окружения.")

# Хранилище: postgres (Supabase, по умолчанию) или sqlite (файл на сервере, фаза 5 U10)
DB_BACKEND = os.getenv("DB_BACKEND", "postgres").strip().lower()
if DB_BACKEND not in ("postgres", "sqlite"):
    raise ValueError(f"DB_BACKEND={DB_BACKEND!r}: ожидается postgres или sqlite")

# Файл SQLite (DB_BACKEND=sqlite)
GRATITUDE_DB = os.path.expanduser(os.getenv("GRATITUDE_DB", "~/state/gratitude.sqlite"))

# URL базы данных PostgreSQL (Supabase), нужен только для DB_BACKEND=postgres
DATABASE_URL = os.getenv("DATABASE_URL")

if DB_BACKEND == "postgres" and not DATABASE_URL:
    raise ValueError("DATABASE_URL не установлен! Добавь его в переменные окружения.")

# ID администраторов (могут смотреть статистику)
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
