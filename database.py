from typing import Optional, List, Dict
import asyncpg
import json
from datetime import datetime
from config import DATABASE_URL


class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self):
        """Инициализация базы данных"""
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reminder_hour INTEGER DEFAULT 21,
                    reminder_minute INTEGER DEFAULT 0,
                    timezone INTEGER DEFAULT 3
                )
            """)

            # Таблица записей благодарностей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    gratitudes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    async def add_user(self, user_id: int) -> bool:
        """Добавить пользователя. Возвращает True если новый."""
        async with self.pool.acquire() as conn:
            # Проверяем, существует ли пользователь
            row = await conn.fetchrow(
                "SELECT user_id FROM users WHERE user_id = $1",
                user_id
            )

            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id) VALUES ($1)",
                    user_id
                )
                return True  # Новый пользователь

            return False  # Уже существует

    async def get_all_users(self) -> List[int]:
        """Получить всех пользователей"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users")
            return [row['user_id'] for row in rows]

    async def save_entry(self, user_id: int, gratitudes: List[str]):
        """Сохранить запись благодарностей (объединяет записи за один день)"""
        today = datetime.now().strftime("%Y-%m-%d")

        async with self.pool.acquire() as conn:
            # Проверяем, есть ли уже запись за сегодня
            row = await conn.fetchrow(
                "SELECT id, gratitudes FROM entries WHERE user_id = $1 AND DATE(created_at) = $2",
                user_id, today
            )

            if row:
                # Добавляем к существующей записи
                existing_gratitudes = json.loads(row['gratitudes'])
                existing_gratitudes.extend(gratitudes)
                await conn.execute(
                    "UPDATE entries SET gratitudes = $1 WHERE id = $2",
                    json.dumps(existing_gratitudes, ensure_ascii=False), row['id']
                )
            else:
                # Создаём новую запись
                await conn.execute(
                    "INSERT INTO entries (user_id, gratitudes, created_at) VALUES ($1, $2, $3)",
                    user_id, json.dumps(gratitudes, ensure_ascii=False), datetime.now()
                )

    async def get_entries(self, user_id: int) -> List[Dict]:
        """Получить все записи пользователя"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT gratitudes, created_at FROM entries WHERE user_id = $1 ORDER BY created_at ASC",
                user_id
            )

            entries = []
            for row in rows:
                entries.append({
                    "gratitudes": json.loads(row['gratitudes']),
                    "date": row['created_at']
                })
            return entries

    async def get_entry_count(self, user_id: int) -> int:
        """Получить количество записей пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) as count FROM entries WHERE user_id = $1",
                user_id
            )
            return row['count']

    async def get_user_time(self, user_id: int) -> Optional[Dict]:
        """Получить время напоминания пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT reminder_hour, reminder_minute FROM users WHERE user_id = $1",
                user_id
            )
            if row:
                return {"hour": row['reminder_hour'], "minute": row['reminder_minute']}
            return None

    async def set_user_time(self, user_id: int, hour: int, minute: int):
        """Установить время напоминания"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET reminder_hour = $1, reminder_minute = $2 WHERE user_id = $3",
                hour, minute, user_id
            )

    async def get_user_timezone(self, user_id: int) -> int:
        """Получить часовой пояс пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT timezone FROM users WHERE user_id = $1",
                user_id
            )
            return row['timezone'] if row else 3  # По умолчанию Москва

    async def set_user_timezone(self, user_id: int, tz_offset: int):
        """Установить часовой пояс"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET timezone = $1 WHERE user_id = $2",
                tz_offset, user_id
            )

    async def get_all_users_with_settings(self) -> List[Dict]:
        """Получить всех пользователей с настройками для напоминаний"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, reminder_hour, reminder_minute, timezone FROM users"
            )
            return [
                {
                    "user_id": row['user_id'],
                    "hour": row['reminder_hour'],
                    "minute": row['reminder_minute'],
                    "timezone": row['timezone'] or 3
                }
                for row in rows
            ]

    async def get_today_entry(self, user_id: int) -> Optional[List[str]]:
        """Получить запись за сегодня (объединённую)"""
        today = datetime.now().strftime("%Y-%m-%d")

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT gratitudes FROM entries WHERE user_id = $1 AND DATE(created_at) = $2",
                user_id, today
            )
            if row:
                return json.loads(row['gratitudes'])
            return None
