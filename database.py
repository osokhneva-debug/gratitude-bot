"""Хранилище ThanksBot: Postgres (asyncpg) или SQLite-файл (aiosqlite).

Выбор - переменная окружения DB_BACKEND (config.py): postgres по умолчанию, sqlite после
переключения (план фазы 5, U10). У обоих классов один набор методов, bot.py не меняется.
Драйверы импортируются лениво: модуль читается без них (схему берёт ops/phase5/move_thanks.py
в venv personal-secretary, где asyncpg и aiosqlite не стоят).
"""
from typing import Optional, List, Dict
import json
import random
from datetime import datetime, date, timedelta


class PostgresDatabase:
    def __init__(self):
        self.pool = None

    async def init(self):
        """Инициализация базы данных"""
        import asyncpg
        from config import DATABASE_URL
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


# ── SQLite ──
# Типы как в Postgres: timestamp без часового пояса - текст 'YYYY-MM-DD HH:MM:SS.ffffff'
# (время сервера, как CURRENT_TIMESTAMP Supabase - UTC), INTEGER[] - JSON-массив, BOOLEAN -
# 0/1. Проверки повторяют строгость Postgres: в integer-колонку не ляжет 5.5 или '3', во время -
# строка, которая не читается как время.
TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
NOW_SQL = "(strftime('%Y-%m-%d %H:%M:%f', 'now'))"

SQLITE_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    created_at TEXT DEFAULT {NOW_SQL},
    reminder_hour INTEGER DEFAULT 21,
    reminder_minute INTEGER DEFAULT 0,
    timezone INTEGER DEFAULT 3,
    shown_quote_ids TEXT DEFAULT '[]',
    CHECK (created_at IS NULL OR datetime(created_at) IS NOT NULL),
    CHECK (reminder_hour IS NULL OR typeof(reminder_hour) = 'integer'),
    CHECK (reminder_minute IS NULL OR typeof(reminder_minute) = 'integer'),
    CHECK (timezone IS NULL OR typeof(timezone) = 'integer'),
    CHECK (shown_quote_ids IS NULL OR json_type(shown_quote_ids) = 'array')
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(user_id),
    gratitudes TEXT,
    created_at TEXT DEFAULT {NOW_SQL},
    CHECK (created_at IS NULL OR datetime(created_at) IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS pending_gratitudes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER REFERENCES users(user_id),
    to_username TEXT NOT NULL,
    gratitude_text TEXT NOT NULL,
    created_at TEXT DEFAULT {NOW_SQL},
    delivered INTEGER DEFAULT 0 CHECK (delivered IN (0, 1))
    CHECK (created_at IS NULL OR datetime(created_at) IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS entries_user_created ON entries (user_id, created_at);
CREATE INDEX IF NOT EXISTS pending_to_username ON pending_gratitudes (to_username, delivered);
"""

# Тот же выбор, что у Postgres: (reminder_hour - timezone + 24) % 24 = utc_hour. Целые числа
# в обеих базах делятся с остатком одинаково (знак остатка по делимому). Дробный пояс в
# integer-колонку не попадает (asyncpg отбрасывает дробь: 5.5 -> 5, здесь int() в методах и
# проверка typeof в схеме); если всё же попал, Postgres numeric даёт
# дробный остаток и не совпадает ни с одним часом, а SQLite округлил бы его до целого -
# поэтому дробные значения исключены явно. Тест: tests/test_database_sqlite.py, 1 440 минут.
REMINDER_SQL = """
    SELECT user_id FROM users
    WHERE reminder_minute = ?
    AND COALESCE(timezone, 3) = CAST(COALESCE(timezone, 3) AS INTEGER)
    AND reminder_hour = CAST(reminder_hour AS INTEGER)
    AND ((reminder_hour - COALESCE(timezone, 3) + 24) % 24) = ?
"""


def ts_text(v: datetime) -> str:
    return v.strftime(TS_FMT)


def ts_value(v) -> Optional[datetime]:
    """Текст времени из SQLite в naive datetime, как asyncpg отдаёт timestamp."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(v)


def connect_sqlite_sync(path: str):
    """Синхронное соединение с теми же настройками (перенос, бэкап, окно)."""
    import sqlite3
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class SqliteDatabase:
    """Те же методы, что у PostgresDatabase, поверх одного соединения aiosqlite.

    Соединение в режиме автокоммита, как asyncpg вне транзакции: каждая команда видна сразу.
    """

    def __init__(self, path: str):
        self.path = path
        self.conn = None

    async def init(self):
        import os
        import aiosqlite
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, timeout=15, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        for pragma in ("journal_mode=WAL", "busy_timeout=15000", "foreign_keys=ON"):
            await self.conn.execute(f"PRAGMA {pragma}")
        await self.conn.executescript(SQLITE_SCHEMA)

    async def close(self):
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def _one(self, sql: str, *args):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, *args):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchall()

    async def _val(self, sql: str, *args):
        row = await self._one(sql, *args)
        return row[0] if row else None

    async def _exec(self, sql: str, *args):
        await self.conn.execute(sql, args)

    async def add_user(self, user_id: int, username: str = None) -> bool:
        row = await self._one("SELECT user_id FROM users WHERE user_id = ?", user_id)
        if not row:
            await self._exec("INSERT INTO users (user_id, username) VALUES (?, ?)",
                             user_id, username.lower() if username else None)
            return True
        if username:
            await self._exec("UPDATE users SET username = ? WHERE user_id = ?",
                             username.lower(), user_id)
        return False

    async def get_all_users(self) -> List[int]:
        return [r["user_id"] for r in await self._all("SELECT user_id FROM users")]

    async def get_stats(self) -> Dict:
        return {"users": await self._val("SELECT COUNT(*) FROM users"),
                "entries": await self._val("SELECT COUNT(*) FROM entries")}

    async def save_entry(self, user_id: int, gratitudes: List[str]):
        today = datetime.now().date()
        row = await self._one(
            "SELECT id, gratitudes FROM entries WHERE user_id = ? AND DATE(created_at) = ?",
            user_id, today.isoformat())
        if row:
            existing = json.loads(row["gratitudes"])
            existing.extend(gratitudes)
            await self._exec("UPDATE entries SET gratitudes = ? WHERE id = ?",
                             json.dumps(existing, ensure_ascii=False), row["id"])
        else:
            await self._exec(
                "INSERT INTO entries (user_id, gratitudes, created_at) VALUES (?, ?, ?)",
                user_id, json.dumps(gratitudes, ensure_ascii=False), ts_text(datetime.now()))

    async def get_entries(self, user_id: int, limit: int = None, offset: int = 0) -> List[Dict]:
        if limit:
            rows = await self._all(
                "SELECT gratitudes, created_at FROM entries WHERE user_id = ? "
                "ORDER BY created_at ASC LIMIT ? OFFSET ?", user_id, limit, offset)
        else:
            rows = await self._all(
                "SELECT gratitudes, created_at FROM entries WHERE user_id = ? "
                "ORDER BY created_at ASC", user_id)
        return [{"gratitudes": json.loads(r["gratitudes"]), "date": ts_value(r["created_at"])}
                for r in rows]

    async def get_entry_count(self, user_id: int) -> int:
        return await self._val("SELECT COUNT(*) FROM entries WHERE user_id = ?", user_id)

    async def get_user_time(self, user_id: int) -> Optional[Dict]:
        row = await self._one(
            "SELECT reminder_hour, reminder_minute FROM users WHERE user_id = ?", user_id)
        if row:
            return {"hour": row["reminder_hour"], "minute": row["reminder_minute"]}
        return None

    async def set_user_time(self, user_id: int, hour: int, minute: int):
        # int(): asyncpg кладёт 5.5 в integer как 5, здесь так же
        await self._exec(
            "UPDATE users SET reminder_hour = ?, reminder_minute = ? WHERE user_id = ?",
            int(hour), int(minute), user_id)

    async def get_user_timezone(self, user_id: int) -> int:
        row = await self._one("SELECT timezone FROM users WHERE user_id = ?", user_id)
        return row["timezone"] if row else 3

    async def set_user_timezone(self, user_id: int, tz_offset: int):
        tz_offset = max(-12, min(14, tz_offset))
        await self._exec("UPDATE users SET timezone = ? WHERE user_id = ?",
                         int(tz_offset), user_id)

    async def get_all_users_with_settings(self) -> List[Dict]:
        rows = await self._all(
            "SELECT user_id, reminder_hour, reminder_minute, timezone FROM users")
        return [{"user_id": r["user_id"], "hour": r["reminder_hour"],
                 "minute": r["reminder_minute"], "timezone": r["timezone"] or 3} for r in rows]

    async def get_users_for_reminder(self, utc_hour: int, utc_minute: int) -> List[int]:
        rows = await self._all(REMINDER_SQL, utc_minute, utc_hour)
        return [r["user_id"] for r in rows]

    async def get_today_entry(self, user_id: int) -> Optional[List[str]]:
        today = datetime.now().date()
        row = await self._one(
            "SELECT gratitudes FROM entries WHERE user_id = ? AND DATE(created_at) = ?",
            user_id, today.isoformat())
        return json.loads(row["gratitudes"]) if row else None

    async def get_streak(self, user_id: int) -> int:
        rows = await self._all(
            "SELECT DISTINCT DATE(created_at) AS entry_date FROM entries WHERE user_id = ? "
            "ORDER BY entry_date DESC", user_id)
        if not rows:
            return 0
        dates = [date.fromisoformat(r["entry_date"]) for r in rows]
        today = datetime.now().date()
        if dates[0] == today:
            current_date = today
        elif dates[0] == today - timedelta(days=1):
            current_date = today - timedelta(days=1)
        else:
            return 0
        streak = 0
        for d in dates:
            if d == current_date:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        return streak

    async def get_random_throwback(self, user_id: int, min_days_ago: int = 7) -> Optional[Dict]:
        cutoff_date = datetime.now().date() - timedelta(days=min_days_ago)
        rows = await self._all(
            "SELECT gratitudes, created_at FROM entries WHERE user_id = ? "
            "AND DATE(created_at) <= ?", user_id, cutoff_date.isoformat())
        if not rows:
            return None
        row = random.choice(rows)
        return {"gratitudes": json.loads(row["gratitudes"]), "date": ts_value(row["created_at"])}

    async def get_total_gratitudes_count(self, user_id: int) -> int:
        result = await self._val(
            "SELECT COALESCE(SUM(json_array_length(gratitudes)), 0) FROM entries "
            "WHERE user_id = ?", user_id)
        return int(result)

    async def get_user_by_username(self, username: str) -> Optional[int]:
        row = await self._one("SELECT user_id FROM users WHERE username = ?",
                              username.lower().lstrip('@'))
        return row["user_id"] if row else None

    async def get_username_by_id(self, user_id: int) -> Optional[str]:
        row = await self._one("SELECT username FROM users WHERE user_id = ?", user_id)
        return row["username"] if row else None

    async def save_pending_gratitude(self, from_user_id: int, to_username: str, text: str):
        await self._exec(
            "INSERT INTO pending_gratitudes (from_user_id, to_username, gratitude_text) "
            "VALUES (?, ?, ?)", from_user_id, to_username.lower().lstrip('@'), text)

    async def get_pending_gratitudes(self, username: str) -> List[Dict]:
        rows = await self._all(
            """SELECT pg.id, pg.from_user_id, pg.gratitude_text, pg.created_at,
                      u.username AS from_username
               FROM pending_gratitudes pg
               JOIN users u ON pg.from_user_id = u.user_id
               WHERE pg.to_username = ? AND pg.delivered = 0
               ORDER BY pg.created_at ASC""", username.lower().lstrip('@'))
        return [{"id": r["id"], "from_user_id": r["from_user_id"],
                 "from_username": r["from_username"], "text": r["gratitude_text"],
                 "date": ts_value(r["created_at"])} for r in rows]

    async def mark_gratitude_delivered(self, gratitude_id: int):
        await self._exec("UPDATE pending_gratitudes SET delivered = 1 WHERE id = ?",
                         gratitude_id)

    async def get_shown_quote_ids(self, user_id: int) -> List[int]:
        row = await self._one("SELECT shown_quote_ids FROM users WHERE user_id = ?", user_id)
        return list(json.loads(row["shown_quote_ids"])) if row and row["shown_quote_ids"] else []

    async def add_shown_quote(self, user_id: int, quote_id: int):
        # array_append(NULL, x) в Postgres даёт {x}, отсюда COALESCE
        await self._exec(
            "UPDATE users SET shown_quote_ids = json_insert(COALESCE(shown_quote_ids, '[]'), "
            "'$[#]', ?) WHERE user_id = ?", quote_id, user_id)

    async def reset_shown_quotes(self, user_id: int):
        await self._exec("UPDATE users SET shown_quote_ids = '[]' WHERE user_id = ?", user_id)

    async def get_active_users_yesterday(self) -> int:
        yesterday = datetime.now().date() - timedelta(days=1)
        result = await self._val(
            "SELECT COUNT(DISTINCT user_id) FROM entries WHERE DATE(created_at) = ?",
            yesterday.isoformat())
        return int(result) if result else 0


def Database():
    """Хранилище по DB_BACKEND: так bot.py создаёт его одной строкой `Database()`."""
    import config
    if config.DB_BACKEND == "sqlite":
        return SqliteDatabase(config.GRATITUDE_DB)
    return PostgresDatabase()
