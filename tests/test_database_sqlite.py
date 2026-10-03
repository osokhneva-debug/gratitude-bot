"""SQLite-хранилище ThanksBot (фаза 5, U10): методы и выбор напоминаний против Postgres.

Подключиться к Postgres бота из теста нельзя, поэтому выражение Postgres перенесено в Python
(pg_reminder_users) по правилам Postgres:
- int4 - int4 + 24 считается в целых без округлений; NULL в любой части даёт NULL, строка
  не выбирается (COALESCE(timezone, 3) закрывает только timezone);
- int4 % int4 - остаток с отбрасыванием дробной части частного, знак по делимому:
  -5 % 24 = -5 (не 19, как в Python);
- numeric % numeric (если бы колонка была numeric и хранила +5:30 как 5.5): тоже
  x - trunc(x / y) * y, но точно и с дробью: 39.5 % 24 = 15.5, такое значение не равно
  ни одному целому часу, напоминание не уходит никогда;
- сравнение с $2 (int4) точное.
Колонка timezone в Postgres - INTEGER, бот пишет только целое (round в bot.py); asyncpg
кладёт 5.5 в int4 как 5 (проверено на настоящем Postgres 03.10), методы SQLite делают так же.
Дробные пояса проверяются отдельно на таблице без проверок типов.

Сверка с настоящим Postgres (встроенный сервер pgserver во временной папке, 03.10): тот же
набор пользователей, 1 440 минут - расхождений 0 между Postgres и SQLite, 0 между Postgres и
pg_reminder_users, 0 на numeric-колонке с дробными и запредельными поясами.
"""
import asyncio
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import database  # noqa: E402

FRACTIONAL = [5.5, 5.75, -3.5, 12.75, 9.5, -9.5, 3.5, 4.5, 6.5, -2.5]
OUT_OF_RANGE = [40, -30, 25, -13, 15]


def pg_mod(a: Decimal, b: Decimal) -> Decimal:
    q = a / b
    q = Decimal(int(q))          # int() у Decimal отбрасывает дробь к нулю, как trunc
    return a - q * b


def pg_reminder_users(users, utc_hour: int, utc_minute: int) -> set:
    out = set()
    for u in users:
        h, m, tz = u["reminder_hour"], u["reminder_minute"], u["timezone"]
        if m is None or m != utc_minute:
            continue
        tz = 3 if tz is None else tz
        if h is None:
            continue
        v = pg_mod(Decimal(str(h)) - Decimal(str(tz)) + 24, Decimal(24))
        if v == utc_hour:
            out.add(u["user_id"])
    return out


def realistic_users(seed: int = 42) -> list:
    rnd = random.Random(seed)
    users, uid = [], 1000
    hours = [7, 8, 9, 10, 12, 18, 19, 20, 21, 21, 21, 22, 22, 23, 0]
    minutes = [0, 0, 0, 0, 15, 30, 30, 45, 5, 59, 1]
    for tz in range(-12, 15):                      # все пояса, которые пропускает бот
        for _ in range(5):
            uid += 1
            users.append({"user_id": uid, "reminder_hour": rnd.choice(hours),
                          "reminder_minute": rnd.choice(minutes), "timezone": tz})
    for h in range(24):                            # края суток во всех часах
        for tz in (-12, 14, 0, 3):
            uid += 1
            users.append({"user_id": uid, "reminder_hour": h,
                          "reminder_minute": (h * 7) % 60, "timezone": tz})
    for _ in range(20):                            # по умолчанию 21:00 Москва и NULL-пояс
        uid += 1
        users.append({"user_id": uid, "reminder_hour": 21, "reminder_minute": 0,
                      "timezone": rnd.choice([3, None])})
    uid += 1
    users.append({"user_id": uid, "reminder_hour": None, "reminder_minute": 0, "timezone": 3})
    uid += 1
    users.append({"user_id": uid, "reminder_hour": 21, "reminder_minute": None, "timezone": 3})
    return users


def odd_users(start: int = 900000) -> list:
    users, uid = [], start
    for tz in FRACTIONAL + OUT_OF_RANGE:
        for h in (0, 3, 9, 15, 21, 23):
            uid += 1
            users.append({"user_id": uid, "reminder_hour": h, "reminder_minute": 30,
                          "timezone": tz})
    return users


def run(coro):
    return asyncio.run(coro)


async def _open(tmp_path):
    db = database.SqliteDatabase(str(tmp_path / "g.sqlite"))
    await db.init()
    return db


def _insert_users(conn, users):
    conn.executemany(
        "INSERT INTO users (user_id, reminder_hour, reminder_minute, timezone) "
        "VALUES (?, ?, ?, ?)",
        [(u["user_id"], u["reminder_hour"], u["reminder_minute"], u["timezone"]) for u in users])


def test_reminder_1440_minutes_match_postgres(tmp_path):
    users = realistic_users()
    conn = database.connect_sqlite_sync(str(tmp_path / "g.sqlite"))
    conn.executescript(database.SQLITE_SCHEMA)
    _insert_users(conn, users)
    conn.close()

    async def body():
        db = database.SqliteDatabase(str(tmp_path / "g.sqlite"))
        await db.init()
        mism, hits = 0, 0
        for minute_of_day in range(1440):
            h, m = divmod(minute_of_day, 60)
            got = set(await db.get_users_for_reminder(h, m))
            want = pg_reminder_users(users, h, m)
            hits += len(want)
            if got != want:
                mism += 1
        await db.close()
        return mism, hits
    mism, hits = run(body())
    print(f"\n1440 минут, {len(users)} пользователей, напоминаний {hits}: расхождений {mism}")
    assert mism == 0
    # каждый пользователь с полными настройками получает ровно одно напоминание в сутки
    assert hits == sum(1 for u in users
                       if u["reminder_hour"] is not None and u["reminder_minute"] is not None)


def test_reminder_fractional_and_out_of_range_offsets(tmp_path):
    """Дробные и запредельные пояса на таблице без проверок типов (как если бы колонка
    была numeric): запрос SQLite совпадает с Postgres; голое выражение Postgres - нет."""
    users = realistic_users() + odd_users()
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, reminder_hour, "
                 "reminder_minute, timezone)")
    _insert_users(conn, users)
    naive = ("SELECT user_id FROM users WHERE reminder_minute = ? "
             "AND ((reminder_hour - COALESCE(timezone, 3) + 24) % 24) = ?")
    mism = naive_mism = 0
    for minute_of_day in range(1440):
        h, m = divmod(minute_of_day, 60)
        want = pg_reminder_users(users, h, m)
        got = {r[0] for r in conn.execute(database.REMINDER_SQL, (m, h))}
        bare = {r[0] for r in conn.execute(naive, (m, h))}
        mism += got != want
        naive_mism += bare != want
    print(f"\nдробные (+5:30, +5:45, -3:30, +12:45 ...) и запредельные пояса: "
          f"расхождений {mism}; без защиты было бы {naive_mism}")
    assert mism == 0
    assert naive_mism > 0


def test_schema_rejects_non_integer_timezone(tmp_path):
    conn = database.connect_sqlite_sync(str(tmp_path / "g.sqlite"))
    conn.executescript(database.SQLITE_SCHEMA)
    for bad in (5.5, "3.5", "abc"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO users (user_id, timezone) VALUES (1, ?)", (bad,))
    conn.execute("INSERT INTO users (user_id, timezone) VALUES (1, '3')")   # '3' станет 3
    assert conn.execute("SELECT typeof(timezone) FROM users").fetchone()[0] == "integer"
    conn.execute("DELETE FROM users")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO users (user_id, created_at) VALUES (2, 'вчера')")
    with pytest.raises(sqlite3.IntegrityError):   # внешний ключ, как в Postgres
        conn.execute("INSERT INTO entries (user_id, gratitudes) VALUES (777, '[]')")


def test_methods_roundtrip(tmp_path):
    async def body():
        db = await _open(tmp_path)
        assert await db.add_user(1, "Olya") is True
        assert await db.add_user(1, "OlyaNew") is False
        assert await db.add_user(2) is True
        assert await db.get_username_by_id(1) == "olyanew"
        assert await db.get_user_by_username("@OlyaNew") == 1
        assert await db.get_user_time(1) == {"hour": 21, "minute": 0}
        assert await db.get_user_timezone(1) == 3
        assert await db.get_user_timezone(999) == 3
        await db.set_user_time(1, 8, 30)
        await db.set_user_timezone(1, 20)
        assert await db.get_user_timezone(1) == 14
        await db.set_user_timezone(2, 5.5)              # как asyncpg: дробь отбрасывается
        assert await db.get_user_timezone(2) == 5
        assert await db.get_users_for_reminder((8 - 14 + 24) % 24, 30) == [1]
        settings = await db.get_all_users_with_settings()
        assert {s["user_id"] for s in settings} == {1, 2}

        await db.save_entry(1, ["солнце", "кофе"])
        await db.save_entry(1, ["друзья"])
        assert await db.get_today_entry(1) == ["солнце", "кофе", "друзья"]
        assert await db.get_entry_count(1) == 1
        assert await db.get_total_gratitudes_count(1) == 3
        assert await db.get_total_gratitudes_count(2) == 0
        entries = await db.get_entries(1)
        assert isinstance(entries[0]["date"], datetime)
        assert entries[0]["date"].date() == datetime.now().date()
        assert len(await db.get_entries(1, limit=1, offset=0)) == 1
        assert await db.get_entries(1, limit=1, offset=1) == []

        # прошлые дни для серии и «год назад»
        now = datetime.now()
        for d in (1, 2, 4, 10):
            await db._exec("INSERT INTO entries (user_id, gratitudes, created_at) "
                           "VALUES (?, ?, ?)", 1, '["x"]',
                           database.ts_text(now - timedelta(days=d)))
        assert await db.get_streak(1) == 3
        assert await db.get_streak(2) == 0
        tb = await db.get_random_throwback(1)
        assert tb["gratitudes"] == ["x"] and isinstance(tb["date"], datetime)
        assert await db.get_random_throwback(2) is None
        assert await db.get_stats() == {"users": 2, "entries": 5}
        assert await db.get_active_users_yesterday() == 1

        await db.save_pending_gratitude(1, "@Masha", "спасибо")
        pend = await db.get_pending_gratitudes("masha")
        assert len(pend) == 1 and pend[0]["from_username"] == "olyanew"
        assert pend[0]["text"] == "спасибо" and isinstance(pend[0]["date"], datetime)
        await db.mark_gratitude_delivered(pend[0]["id"])
        assert await db.get_pending_gratitudes("masha") == []

        assert await db.get_shown_quote_ids(1) == []
        await db.add_shown_quote(1, 5)
        await db.add_shown_quote(1, 7)
        assert await db.get_shown_quote_ids(1) == [5, 7]
        await db._exec("UPDATE users SET shown_quote_ids = NULL WHERE user_id = 2")
        await db.add_shown_quote(2, 3)                 # array_append(NULL, 3) = {3}
        assert await db.get_shown_quote_ids(2) == [3]
        await db.reset_shown_quotes(1)
        assert await db.get_shown_quote_ids(1) == []
        await db.close()
    run(body())


def test_factory_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "x")
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("GRATITUDE_DB", str(tmp_path / "f.sqlite"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    sys.modules.pop("config", None)
    db = database.Database()
    assert isinstance(db, database.SqliteDatabase) and db.path == str(tmp_path / "f.sqlite")
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    sys.modules.pop("config", None)
    assert isinstance(database.Database(), database.PostgresDatabase)
    sys.modules.pop("config", None)
