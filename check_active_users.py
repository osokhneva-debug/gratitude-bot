#!/usr/bin/env python3
"""
Скрипт для подсчета активных пользователей за вчерашний день
"""
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

from database import Database

async def main():
    db = Database()
    await db.init()

    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    active_users = await db.get_active_users_yesterday()

    print(f"📊 Активные пользователи за {yesterday}: {active_users}")

    # Также получим общую статистику
    stats = await db.get_stats()
    print(f"\n📈 Общая статистика:")
    print(f"   Всего пользователей: {stats['users']}")
    print(f"   Всего записей: {stats['entries']}")

    await db.pool.close()

if __name__ == "__main__":
    asyncio.run(main())
