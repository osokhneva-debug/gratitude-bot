from typing import Optional, List, Dict
import aiosqlite
import json
from datetime import datetime
from config import DATABASE_PATH


class Database:
    def __init__(self):
        self.db_path = DATABASE_PATH

    async def init(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            # Таблица пользователей
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    reminder_hour INTEGER DEFAULT 21,
                    reminder_minute INTEGER DEFAULT 0,
                    timezone INTEGER DEFAULT 3
                )
            """)

            # Таблица записей благодарностей
            await db.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    gratitudes TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            """)

            # Миграция: добавляем колонку timezone если её нет
            try:
                await db.execute("ALTER TABLE users ADD COLUMN timezone INTEGER DEFAULT 3")
            except:
                pass  # Колонка уже существует

            await db.commit()

    async def add_user(self, user_id: int) -> bool:
        """Добавить пользователя. Возвращает True если новый."""
        async with aiosqlite.connect(self.db_path) as db:
            # Проверяем, существует ли пользователь
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (user_id,)
            )
            exists = await cursor.fetchone()

            if not exists:
                await db.execute(
                    "INSERT INTO users (user_id) VALUES (?)",
                    (user_id,)
                )
                await db.commit()
                return True  # Новый пользователь

            return False  # Уже существует

    async def get_all_users(self) -> List[int]:
        """Получить всех пользователей"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT user_id FROM users")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def save_entry(self, user_id: int, gratitudes: List[str]):
        """Сохранить запись благодарностей (объединяет записи за один день)"""
        today = datetime.now().strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            # Проверяем, есть ли уже запись за сегодня
            cursor = await db.execute(
                "SELECT id, gratitudes FROM entries WHERE user_id = ? AND date(created_at) = ?",
                (user_id, today)
            )
            existing = await cursor.fetchone()

            if existing:
                # Добавляем к существующей записи
                existing_gratitudes = json.loads(existing[1])
                existing_gratitudes.extend(gratitudes)
                await db.execute(
                    "UPDATE entries SET gratitudes = ? WHERE id = ?",
                    (json.dumps(existing_gratitudes, ensure_ascii=False), existing[0])
                )
            else:
                # Создаём новую запись
                await db.execute(
                    "INSERT INTO entries (user_id, gratitudes, created_at) VALUES (?, ?, ?)",
                    (user_id, json.dumps(gratitudes, ensure_ascii=False), datetime.now().isoformat())
                )
            await db.commit()

    async def get_entries(self, user_id: int) -> List[Dict]:
        """Получить все записи пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT gratitudes, created_at FROM entries WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,)
            )
            rows = await cursor.fetchall()

            entries = []
            for row in rows:
                entries.append({
                    "gratitudes": json.loads(row[0]),
                    "date": datetime.fromisoformat(row[1])
                })
            return entries

    async def get_entry_count(self, user_id: int) -> int:
        """Получить количество записей пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM entries WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            return row[0]

    async def get_user_time(self, user_id: int) -> Optional[Dict]:
        """Получить время напоминания пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT reminder_hour, reminder_minute FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                return {"hour": row[0], "minute": row[1]}
            return None

    async def set_user_time(self, user_id: int, hour: int, minute: int):
        """Установить время напоминания"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET reminder_hour = ?, reminder_minute = ? WHERE user_id = ?",
                (hour, minute, user_id)
            )
            await db.commit()

    async def get_user_timezone(self, user_id: int) -> int:
        """Получить часовой пояс пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT timezone FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 3  # По умолчанию Москва

    async def set_user_timezone(self, user_id: int, tz_offset: int):
        """Установить часовой пояс"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET timezone = ? WHERE user_id = ?",
                (tz_offset, user_id)
            )
            await db.commit()

    async def get_all_users_with_settings(self) -> List[Dict]:
        """Получить всех пользователей с настройками для напоминаний"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT user_id, reminder_hour, reminder_minute, timezone FROM users"
            )
            rows = await cursor.fetchall()
            return [
                {
                    "user_id": row[0],
                    "hour": row[1],
                    "minute": row[2],
                    "timezone": row[3] or 3
                }
                for row in rows
            ]

    async def get_today_entry(self, user_id: int) -> Optional[List[str]]:
        """Получить запись за сегодня (объединённую)"""
        today = datetime.now().strftime("%Y-%m-%d")

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT gratitudes FROM entries WHERE user_id = ? AND date(created_at) = ?",
                (user_id, today)
            )
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
            return None
