# ThanksBot — Telegram Gratitude Bot

## Overview
Telegram-бот для практики благодарности. Записывает благодарности, хранит в Supabase, отправляет мотивационные напоминания, генерирует PDF-отчёты.

## Tech Stack
- Python 3, aiogram (async), APScheduler
- PostgreSQL (Supabase)
- ReportLab (PDF с русским шрифтом DejaVuSans.ttf)

## Run
```bash
python bot.py
```
Requires `.env`: BOT_TOKEN, DATABASE_URL, ADMIN_IDS

## Structure
- `bot.py` — main bot (handlers, FSM, PDF generation, motivational quotes)
- `config.py` — env vars
- `database.py` — PostgreSQL operations
- `gratitude.db` — local SQLite fallback

## Вторая память (SecondBrain)
- Общий контекст Оли: ~/SecondBrain/BRAIN.md (читать первым)
- Карточка этого проекта: ~/SecondBrain/60-projects/thanks-bot.md (статус, решения, следующий шаг — ОБНОВЛЯТЬ после значимых изменений)
- Контекст других проектов не подтягивать без явной просьбы
