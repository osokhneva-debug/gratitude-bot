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
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reminder_hour INTEGER DEFAULT 21,
                    reminder_minute INTEGER DEFAULT 0,
                    timezone INTEGER DEFAULT 3
                )
            """)

            # Добавляем колонку username если её нет (миграция)
            await conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='users' AND column_name='username') THEN
                        ALTER TABLE users ADD COLUMN username TEXT;
                    END IF;
                END $$;
            """)

            # Добавляем колонку shown_quote_ids для мотивационных цитат (миграция)
            await conn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='users' AND column_name='shown_quote_ids') THEN
                        ALTER TABLE users ADD COLUMN shown_quote_ids INTEGER[] DEFAULT '{}';
                    END IF;
                END $$;
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

            # Таблица отложенных благодарностей (для тех, кто ещё не в боте)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_gratitudes (
                    id SERIAL PRIMARY KEY,
                    from_user_id BIGINT REFERENCES users(user_id),
                    to_username TEXT NOT NULL,
                    gratitude_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    delivered BOOLEAN DEFAULT FALSE
                )
            """)

    async def add_user(self, user_id: int, username: str = None) -> bool:
        """Добавить пользователя. Возвращает True если новый."""
        async with self.pool.acquire() as conn:
            # Проверяем, существует ли пользователь
            row = await conn.fetchrow(
                "SELECT user_id FROM users WHERE user_id = $1",
                user_id
            )

            if not row:
                await conn.execute(
                    "INSERT INTO users (user_id, username) VALUES ($1, $2)",
                    user_id, username.lower() if username else None
                )
                return True  # Новый пользователь
            else:
                # Обновляем username если изменился
                if username:
                    await conn.execute(
                        "UPDATE users SET username = $1 WHERE user_id = $2",
                        username.lower(), user_id
                    )

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

    async def get_entries(self, user_id: int, limit: int = None, offset: int = 0) -> List[Dict]:
        """Получить записи пользователя с пагинацией"""
        async with self.pool.acquire() as conn:
            if limit:
                rows = await conn.fetch(
                    "SELECT gratitudes, created_at FROM entries WHERE user_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3",
                    user_id, limit, offset
                )
            else:
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
        """Установить часовой пояс (с валидацией)"""
        # Валидация: UTC-12 до UTC+14
        tz_offset = max(-12, min(14, tz_offset))
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

    async def get_users_for_reminder(self, utc_hour: int, utc_minute: int) -> List[int]:
        """Получить пользователей, которым нужно отправить напоминание сейчас (оптимизировано)"""
        async with self.pool.acquire() as conn:
            # Фильтруем в SQL: (reminder_hour - timezone) mod 24 = utc_hour
            rows = await conn.fetch(
                """
                SELECT user_id FROM users
                WHERE reminder_minute = $1
                AND ((reminder_hour - COALESCE(timezone, 3) + 24) % 24) = $2
                """,
                utc_minute, utc_hour
            )
            return [row['user_id'] for row in rows]

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
        """Получить общее количество благодарностей (оптимизировано через JSON)"""
        async with self.pool.acquire() as conn:
            # Считаем количество элементов в JSON массиве прямо в SQL
            result = await conn.fetchval(
                """
                SELECT COALESCE(SUM(json_array_length(gratitudes::json)), 0)
                FROM entries WHERE user_id = $1
                """,
                user_id
            )
            return int(result)

    async def get_user_by_username(self, username: str) -> Optional[int]:
        """Найти user_id по username"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM users WHERE username = $1",
                username.lower().lstrip('@')
            )
            return row['user_id'] if row else None

    async def get_username_by_id(self, user_id: int) -> Optional[str]:
        """Получить username по user_id"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT username FROM users WHERE user_id = $1",
                user_id
            )
            return row['username'] if row else None

    async def save_pending_gratitude(self, from_user_id: int, to_username: str, text: str):
        """Сохранить отложенную благодарность для пользователя, которого нет в боте"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO pending_gratitudes (from_user_id, to_username, gratitude_text)
                   VALUES ($1, $2, $3)""",
                from_user_id, to_username.lower().lstrip('@'), text
            )

    async def get_pending_gratitudes(self, username: str) -> List[Dict]:
        """Получить все отложенные благодарности для пользователя"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT pg.id, pg.from_user_id, pg.gratitude_text, pg.created_at, u.username as from_username
                   FROM pending_gratitudes pg
                   JOIN users u ON pg.from_user_id = u.user_id
                   WHERE pg.to_username = $1 AND pg.delivered = FALSE
                   ORDER BY pg.created_at ASC""",
                username.lower().lstrip('@')
            )
            return [
                {
                    "id": row['id'],
                    "from_user_id": row['from_user_id'],
                    "from_username": row['from_username'],
                    "text": row['gratitude_text'],
                    "date": row['created_at']
                }
                for row in rows
            ]

    async def mark_gratitude_delivered(self, gratitude_id: int):
        """Отметить благодарность как доставленную"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE pending_gratitudes SET delivered = TRUE WHERE id = $1",
                gratitude_id
            )

    async def get_shown_quote_ids(self, user_id: int) -> List[int]:
        """Получить список ID показанных цитат"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT shown_quote_ids FROM users WHERE user_id = $1",
                user_id
            )
            return list(row['shown_quote_ids']) if row and row['shown_quote_ids'] else []

    async def add_shown_quote(self, user_id: int, quote_id: int):
        """Добавить ID показанной цитаты"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE users
                SET shown_quote_ids = array_append(shown_quote_ids, $1)
                WHERE user_id = $2
                """,
                quote_id, user_id
            )

    async def reset_shown_quotes(self, user_id: int):
        """Сбросить список показанных цитат (когда все показаны)"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET shown_quote_ids = '{}' WHERE user_id = $1",
                user_id
            )

    async def get_active_users_yesterday(self) -> int:
        """Получить количество активных пользователей за вчерашний день"""
        async with self.pool.acquire() as conn:
            yesterday = datetime.now().date() - timedelta(days=1)
            result = await conn.fetchval(
                """
                SELECT COUNT(DISTINCT user_id)
                FROM entries
                WHERE DATE(created_at) = $1
                """,
                yesterday
            )
            return int(result) if result else 0
