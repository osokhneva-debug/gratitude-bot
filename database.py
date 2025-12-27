from typing import Optional, List, Dict
import asyncpg
import json
import random
from datetime import datetime, timedelta
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

    async def get_stats(self) -> Dict:
        """Получить статистику для админки"""
        async with self.pool.acquire() as conn:
            users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            entries_count = await conn.fetchval("SELECT COUNT(*) FROM entries")
            return {
                "users": users_count,
                "entries": entries_count
            }

    async def save_entry(self, user_id: int, gratitudes: List[str]):
        """Сохранить запись благодарностей (объединяет записи за один день)"""
        today = datetime.now().date()

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
        today = datetime.now().date()

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT gratitudes FROM entries WHERE user_id = $1 AND DATE(created_at) = $2",
                user_id, today
            )
            if row:
                return json.loads(row['gratitudes'])
            return None

    async def get_streak(self, user_id: int) -> int:
        """Подсчитать текущую серию дней подряд с записями"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT DATE(created_at) as entry_date FROM entries WHERE user_id = $1 ORDER BY entry_date DESC",
                user_id
            )

            if not rows:
                return 0

            dates = [row['entry_date'] for row in rows]
            today = datetime.now().date()

            # Если сегодня нет записи, проверяем со вчера
            if dates[0] == today:
                current_date = today
            elif dates[0] == today - timedelta(days=1):
                current_date = today - timedelta(days=1)
            else:
                return 0  # Серия прервана

            streak = 0
            for d in dates:
                if d == current_date:
                    streak += 1
                    current_date -= timedelta(days=1)
                else:
                    break

            return streak

    async def get_random_throwback(self, user_id: int, min_days_ago: int = 7) -> Optional[Dict]:
        """Получить случайную запись старше min_days_ago дней"""
        cutoff_date = datetime.now().date() - timedelta(days=min_days_ago)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT gratitudes, created_at FROM entries WHERE user_id = $1 AND DATE(created_at) <= $2",
                user_id, cutoff_date
            )

            if not rows:
                return None

            row = random.choice(rows)
            return {
                "gratitudes": json.loads(row['gratitudes']),
                "date": row['created_at']
            }

    async def get_total_gratitudes_count(self, user_id: int) -> int:
        """Получить общее количество благодарностей (не записей, а самих благодарностей)"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT gratitudes FROM entries WHERE user_id = $1",
                user_id
            )

            total = 0
            for row in rows:
                gratitudes = json.loads(row['gratitudes'])
                total += len(gratitudes)
            return total
