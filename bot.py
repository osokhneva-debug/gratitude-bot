import asyncio
import logging
import os
import signal
from datetime import datetime, timezone, timedelta
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BotCommand
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import Database
from config import BOT_TOKEN, ADMIN_IDS

# Логирование
logging.basicConfig(level=logging.INFO)

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = Database()
scheduler = AsyncIOScheduler()



# Главное меню с кнопками
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📝 Записать"), KeyboardButton(text="📖 Дневник")],
        [KeyboardButton(text="⏰ Настройки")]
    ],
    resize_keyboard=True
)

# Кнопки при записи благодарностей
write_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💾 Сохранить"), KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)


# Состояния FSM
class GratitudeStates(StatesGroup):
    waiting_for_current_time = State()  # Ожидание ввода текущего времени для расчёта часового пояса
    waiting_for_gratitudes = State()
    waiting_for_time = State()


def parse_time(text: str) -> tuple[int, int]:
    """Парсинг времени в разных форматах: 12:30, 12.30, 12 30, 1230"""
    text = text.strip()

    # Пробуем разные разделители
    for sep in [":", ".", " "]:
        if sep in text:
            parts = text.split(sep)
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return hour, minute

    # Без разделителя: 1230 -> 12:30, 930 -> 9:30
    if text.isdigit():
        if len(text) == 4:
            return int(text[:2]), int(text[2:])
        elif len(text) == 3:
            return int(text[0]), int(text[1:])
        elif len(text) <= 2:
            return int(text), 0

    raise ValueError("Cannot parse time")


# ==================== ХЕНДЛЕРЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Приветствие и онбординг"""
    is_new = await db.add_user(message.from_user.id)

    if is_new:
        # Новый пользователь — показываем онбординг
        await message.answer(
            "🙏 Привет! Я — твой Дневник Благодарностей.\n\n"
            "Практика благодарности помогает замечать хорошее "
            "в жизни и чувствовать себя счастливее.\n\n"
            "Каждый день я буду напоминать тебе записать, "
            "за что ты благодарен. Это займёт пару минут.",
            reply_markup=ReplyKeyboardRemove()
        )

        # Запрашиваем часовой пояс
        await asyncio.sleep(1)
        await ask_timezone(message, state)
    else:
        # Вернувшийся пользователь
        await message.answer(
            "С возвращением! 🙏\n\n"
            "Выбери действие:",
            reply_markup=main_menu
        )


async def ask_timezone(message: Message, state: FSMContext):
    """Запрос текущего времени для определения часового пояса"""
    await state.set_state(GratitudeStates.waiting_for_current_time)
    await message.answer(
        "🕐 Сколько сейчас у тебя времени?\n\n"
        "Напиши в формате ЧЧ:ММ, например: 14:30",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(GratitudeStates.waiting_for_current_time)
async def process_current_time(message: Message, state: FSMContext):
    """Обработка ввода текущего времени для расчёта часового пояса"""
    try:
        user_hour, user_minute = parse_time(message.text)

        if not (0 <= user_hour <= 23 and 0 <= user_minute <= 59):
            raise ValueError("Invalid time")

        # Получаем текущее UTC время
        utc_now = datetime.now(timezone.utc)

        # Вычисляем смещение: разница между временем пользователя и UTC
        user_total_minutes = user_hour * 60 + user_minute
        utc_total_minutes = utc_now.hour * 60 + utc_now.minute

        diff_minutes = user_total_minutes - utc_total_minutes

        # Корректируем если разница больше 12 часов (переход через полночь)
        if diff_minutes > 720:  # > 12 часов
            diff_minutes -= 1440  # -24 часа
        elif diff_minutes < -720:  # < -12 часов
            diff_minutes += 1440  # +24 часа

        # Округляем до целого часа
        offset = round(diff_minutes / 60)

        # Ограничиваем диапазон UTC-12 до UTC+14
        offset = max(-12, min(14, offset))

        await db.set_user_timezone(message.from_user.id, offset)

        # Форматируем отображение
        if offset >= 0:
            tz_display = f"UTC+{offset}"
        else:
            tz_display = f"UTC{offset}"

        await message.answer(
            f"✅ Отлично! Твой часовой пояс: {tz_display}\n\n"
            f"Напоминания будут приходить в 21:00 по твоему времени.\n"
            f"Это можно изменить в настройках."
        )

        await state.clear()
        await asyncio.sleep(1)

        await message.answer(
            "Теперь ты готов начать!\n\n"
            "Нажми 📝 Записать, чтобы добавить первую запись.",
            reply_markup=main_menu
        )

    except (ValueError, IndexError):
        await message.answer(
            "❌ Неверный формат. Напиши время, например: 14:30 или 9.00"
        )


@dp.message(Command("write"))
@dp.message(F.text == "📝 Записать")
async def cmd_write(message: Message, state: FSMContext):
    """Начать запись благодарностей"""
    await state.set_state(GratitudeStates.waiting_for_gratitudes)
    await state.update_data(gratitudes=[])
    await message.answer(
        "✨ За что ты благодарен сегодня?\n\n"
        "Напиши и отправь сообщение (можно списком, каждая с новой строки).",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    current_state = await state.get_state()

    if current_state:
        await state.clear()
        await message.answer(
            "Действие отменено.",
            reply_markup=main_menu
        )
    else:
        await message.answer(
            "Нечего отменять 🤷",
            reply_markup=main_menu
        )


@dp.message(F.text == "💾 Сохранить", GratitudeStates.waiting_for_gratitudes)
async def save_gratitudes(message: Message, state: FSMContext):
    """Сохранить благодарности"""
    data = await state.get_data()
    gratitudes = data.get("gratitudes", [])

    if not gratitudes:
        await message.answer("📭 Ты ещё ничего не написал. Напиши хотя бы одну благодарность!")
        return

    # Сохраняем в базу (объединяет с существующими за сегодня)
    await db.save_entry(message.from_user.id, gratitudes)

    # Получаем объединённый список за сегодня для отображения
    all_today = await db.get_today_entry(message.from_user.id)

    # Получаем количество записей для поздравления
    count = await db.get_entry_count(message.from_user.id)

    # Формируем красивую карточку с ПОЛНЫМ списком за день
    card = format_card(all_today, datetime.now())

    # Сообщение с поздравлением для круглых чисел
    congrats = ""
    if count == 1:
        congrats = "\n\n🎉 Это твоя первая запись! Отличное начало!"
    elif count == 7:
        congrats = "\n\n🔥 Неделя благодарностей! Так держать!"
    elif count == 30:
        congrats = "\n\n🏆 30 дней! Ты формируешь привычку!"
    elif count % 10 == 0:
        congrats = f"\n\n⭐ {count} записей! Отличный результат!"

    await state.clear()

    # Показываем добавленное количество и общее за день
    added = len(gratitudes)
    total = len(all_today)
    if added == total:
        count_msg = f"{total} благодарностей"
    else:
        count_msg = f"+{added}, всего за день: {total}"

    await message.answer(
        f"🎉 Запись сохранена! ({count_msg}){congrats}\n\n{card}",
        reply_markup=main_menu
    )


@dp.callback_query(F.data == "save_gratitudes")
async def save_gratitudes_inline(callback: CallbackQuery, state: FSMContext):
    """Сохранить благодарности (inline-кнопка)"""
    data = await state.get_data()
    gratitudes = data.get("gratitudes", [])

    if not gratitudes:
        await callback.answer("Сначала напиши хотя бы одну благодарность!", show_alert=True)
        return

    # Сохраняем в базу
    await db.save_entry(callback.from_user.id, gratitudes)

    # Получаем данные для отображения
    all_today = await db.get_today_entry(callback.from_user.id)
    count = await db.get_entry_count(callback.from_user.id)
    card = format_card(all_today, datetime.now())

    # Поздравления
    congrats = ""
    if count == 1:
        congrats = "\n\n🎉 Это твоя первая запись! Отличное начало!"
    elif count == 7:
        congrats = "\n\n🔥 Неделя благодарностей! Так держать!"
    elif count == 30:
        congrats = "\n\n🏆 30 дней! Ты формируешь привычку!"
    elif count % 10 == 0:
        congrats = f"\n\n⭐ {count} записей! Отличный результат!"

    await state.clear()

    added = len(gratitudes)
    total = len(all_today)
    count_msg = f"{total} благодарностей" if added == total else f"+{added}, всего за день: {total}"

    # Убираем inline-кнопки из предыдущего сообщения
    await callback.message.edit_reply_markup(reply_markup=None)

    await callback.message.answer(
        f"🎉 Запись сохранена! ({count_msg}){congrats}\n\n{card}",
        reply_markup=main_menu
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel_gratitudes")
async def cancel_gratitudes_inline(callback: CallbackQuery, state: FSMContext):
    """Отмена записи (inline-кнопка)"""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Действие отменено.", reply_markup=main_menu)
    await callback.answer()


@dp.message(GratitudeStates.waiting_for_gratitudes)
async def process_gratitude(message: Message, state: FSMContext):
    """Обработка ввода благодарностей"""
    # Игнорируем команды — они не должны добавляться как благодарности
    if message.text and message.text.startswith('/'):
        return

    data = await state.get_data()
    gratitudes = data.get("gratitudes", [])

    # Разбиваем на строки, если пользователь прислал список
    new_items = [line.strip() for line in message.text.split("\n") if line.strip()]
    gratitudes.extend(new_items)

    await state.update_data(gratitudes=gratitudes)

    # Inline-кнопки для сохранения/отмены
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💾 Сохранить", callback_data="save_gratitudes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_gratitudes")
        ]
    ])

    await message.answer(
        f"✓ Записано: {len(gratitudes)}\n\n"
        "Продолжай писать или сохрани.",
        reply_markup=inline_kb
    )


@dp.message(Command("diary"))
@dp.message(F.text == "📖 Дневник")
async def cmd_diary(message: Message):
    """Показать архив записей со статистикой"""
    entries = await db.get_entries(message.from_user.id)

    if not entries:
        await message.answer(
            "📭 У тебя пока нет записей.\n\nНажми 📝 Записать чтобы начать!",
            reply_markup=main_menu
        )
        return

    # Получаем статистику
    streak = await db.get_streak(message.from_user.id)
    total_gratitudes = await db.get_total_gratitudes_count(message.from_user.id)
    total_days = len(entries)

    # Формируем шапку со статистикой
    streak_emoji = "🔥" if streak > 0 else "💤"
    stats_header = (
        f"📊 <b>Твоя статистика</b>\n"
        f"{streak_emoji} Серия: {streak} дней подряд\n"
        f"📝 Записей: {total_days} | Благодарностей: {total_gratitudes}\n"
    )

    # Проверяем throwback (случайная старая запись)
    throwback = await db.get_random_throwback(message.from_user.id)
    throwback_text = ""
    if throwback:
        tb_date = throwback["date"].strftime("%d.%m.%Y")
        tb_sample = throwback["gratitudes"][0][:50]
        if len(throwback["gratitudes"][0]) > 50:
            tb_sample += "..."
        throwback_text = f"\n💫 <b>Воспоминание ({tb_date}):</b>\n<i>«{tb_sample}»</i>\n"

    await message.answer(
        f"{stats_header}{throwback_text}\n─────────────────",
        parse_mode="HTML",
        reply_markup=main_menu
    )

    # Показываем последнюю запись с кнопками навигации
    await show_entry(message, entries, len(entries) - 1)


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админская панель — только для админов"""
    if message.from_user.id not in ADMIN_IDS:
        return  # Молча игнорируем для не-админов

    stats = await db.get_stats()
    user_ids = await db.get_all_users()

    # Получаем информацию о пользователях
    users_info = []
    for user_id in user_ids:
        try:
            chat = await bot.get_chat(user_id)
            name = chat.full_name or "Без имени"
            username = f"@{chat.username}" if chat.username else ""
            users_info.append(f"• {name} {username} (ID: {user_id})")
        except:
            users_info.append(f"• ID: {user_id}")

    users_list = "\n".join(users_info) if users_info else "Пока нет пользователей"

    await message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"📝 Записей: {stats['entries']}\n\n"
        f"<b>Пользователи:</b>\n{users_list}",
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Показать справку"""
    await message.answer(
        "🙏 <b>Дневник Благодарностей</b>\n\n"
        "Этот бот помогает вести практику благодарности — "
        "каждый день записывать, за что ты благодарен.\n\n"
        "<b>Команды:</b>\n"
        "/start — перезапустить бота\n"
        "/write — записать благодарности\n"
        "/diary — открыть дневник\n"
        "/settings — настройки\n"
        "/help — эта справка\n\n"
        "<b>Как пользоваться:</b>\n"
        "1. Нажми 📝 Записать\n"
        "2. Напиши за что благодарен (можно списком)\n"
        "3. Нажми 💾 Сохранить\n\n"
        "Бот будет напоминать тебе каждый день в выбранное время.",
        parse_mode="HTML",
        reply_markup=main_menu
    )


@dp.message(Command("settings"))
@dp.message(F.text == "⏰ Настройки")
async def cmd_settings(message: Message):
    """Показать настройки"""
    user_time = await db.get_user_time(message.from_user.id)
    user_tz = await db.get_user_timezone(message.from_user.id)

    current_time = f"{user_time['hour']:02d}:{user_time['minute']:02d}" if user_time else "21:00"
    current_tz = f"UTC+{user_tz}" if user_tz else "UTC+3"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ Изменить время", callback_data="settings_time")],
        [InlineKeyboardButton(text="🌍 Изменить часовой пояс", callback_data="settings_tz")],
    ])

    await message.answer(
        f"⚙️ Настройки\n\n"
        f"🕐 Время напоминания: {current_time}\n"
        f"🌍 Часовой пояс: {current_tz}",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "settings_time")
async def settings_time(callback: CallbackQuery, state: FSMContext):
    """Изменение времени напоминания"""
    time_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌅 9:00", callback_data="time_9_0"),
            InlineKeyboardButton(text="☀️ 12:00", callback_data="time_12_0"),
        ],
        [
            InlineKeyboardButton(text="🌆 18:00", callback_data="time_18_0"),
            InlineKeyboardButton(text="🌙 21:00", callback_data="time_21_0"),
        ],
        [
            InlineKeyboardButton(text="🌚 22:00", callback_data="time_22_0"),
            InlineKeyboardButton(text="✍️ Своё время", callback_data="time_custom"),
        ]
    ])

    await callback.message.edit_text(
        "⏰ Выбери время напоминания:",
        reply_markup=time_keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "settings_tz")
async def settings_timezone(callback: CallbackQuery, state: FSMContext):
    """Изменение часового пояса"""
    await state.set_state(GratitudeStates.waiting_for_current_time)
    await callback.message.answer(
        "🕐 Сколько сейчас у тебя времени?\n\n"
        "Напиши в формате ЧЧ:ММ, например: 14:30"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("time_"))
async def set_time(callback: CallbackQuery, state: FSMContext):
    """Установка времени напоминания"""
    data = callback.data

    if data == "time_custom":
        await state.set_state(GratitudeStates.waiting_for_time)
        await callback.message.answer(
            "Напиши время в формате ЧЧ:ММ\n"
            "Например: 20:30",
            reply_markup=ReplyKeyboardRemove()
        )
        await callback.answer()
        return

    # Парсим время из callback_data (time_21_0 -> hour=21, minute=0)
    parts = data.split("_")
    hour, minute = int(parts[1]), int(parts[2])

    await db.set_user_time(callback.from_user.id, hour, minute)
    await callback.message.edit_text(f"✅ Напоминание будет приходить в {hour:02d}:{minute:02d}")
    await callback.answer()


@dp.message(GratitudeStates.waiting_for_time)
async def process_custom_time(message: Message, state: FSMContext):
    """Обработка ввода своего времени"""
    try:
        hour, minute = parse_time(message.text)

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Invalid time")

        await db.set_user_time(message.from_user.id, hour, minute)
        await state.clear()
        await message.answer(
            f"✅ Напоминание будет приходить в {hour:02d}:{minute:02d}",
            reply_markup=main_menu
        )
    except (ValueError, IndexError):
        await message.answer("❌ Неверный формат. Напиши время, например: 21:00 или 9.30")


async def show_entry(message: Message, entries: list, index: int):
    """Показать запись с пагинацией"""
    entry = entries[index]
    card = format_card(entry["gratitudes"], entry["date"])

    # Кнопки навигации
    buttons = []
    if index > 0:
        buttons.append(InlineKeyboardButton(text="← Раньше", callback_data=f"page_{index - 1}"))
    if index < len(entries) - 1:
        buttons.append(InlineKeyboardButton(text="Позже →", callback_data=f"page_{index + 1}"))

    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    text = f"📖 Запись {index + 1} из {len(entries)}\n\n{card}"

    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=keyboard)
    else:
        await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("page_"))
async def paginate_diary(callback: CallbackQuery):
    """Листание архива"""
    index = int(callback.data.split("_")[1])
    entries = await db.get_entries(callback.from_user.id)
    await show_entry(callback, entries, index)
    await callback.answer()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def format_card(gratitudes: list, date: datetime) -> str:
    """Форматирует запись в красивую карточку"""
    if isinstance(date, str):
        date = datetime.fromisoformat(date)

    date_str = date.strftime("%d.%m.%Y")

    lines = [f"📅 {date_str}", "─" * 20]
    for i, item in enumerate(gratitudes, 1):
        lines.append(f"{i}. {item}")
    lines.append("─" * 20)

    return "\n".join(lines)


async def send_reminders():
    """Отправка напоминаний с учётом часовых поясов"""
    users = await db.get_all_users_with_settings()
    utc_now = datetime.now(timezone.utc)

    logging.info(f"Checking reminders, UTC now: {utc_now.hour}:{utc_now.minute:02d}, users: {len(users)}")

    for user in users:
        # Вычисляем локальное время пользователя
        user_tz = timezone(timedelta(hours=user['timezone']))
        user_local_time = utc_now.astimezone(user_tz)

        logging.info(
            f"User {user['user_id']}: local={user_local_time.hour}:{user_local_time.minute:02d}, "
            f"reminder={user['hour']}:{user['minute']:02d}, tz={user['timezone']}"
        )

        # Проверяем, совпадает ли текущее время с временем напоминания
        if (user_local_time.hour == user['hour'] and
            user_local_time.minute == user['minute']):
            try:
                logging.info(f"MATCH! Sending reminder to {user['user_id']}")
                await bot.send_message(
                    user['user_id'],
                    "🌙 Привет!\n\n"
                    "За что ты благодарен сегодня?",
                    reply_markup=main_menu
                )
            except Exception as e:
                logging.error(f"Не удалось отправить напоминание {user['user_id']}: {e}")


# ==================== ЗАПУСК ====================

# Простой health-check эндпоинт для Render
async def health_check(request):
    return web.Response(text="OK")


async def shutdown(sig, loop):
    """Корректное завершение при получении сигнала"""
    logging.info(f"Получен сигнал {sig.name}, завершаем работу...")

    # Останавливаем polling
    await dp.stop_polling()

    # Останавливаем планировщик
    scheduler.shutdown(wait=False)

    # Закрываем сессию бота
    await bot.session.close()

    logging.info("Бот остановлен корректно")


async def main():
    # Инициализация БД
    await db.init()

    # Настройка команд меню бота
    await bot.set_my_commands([
        BotCommand(command="start", description="Перезапустить бота"),
        BotCommand(command="write", description="Записать благодарности"),
        BotCommand(command="diary", description="Открыть дневник"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="help", description="Помощь"),
    ])

    # Настройка обработки сигналов для graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown(s, loop))
        )

    # Настройка напоминаний (проверяем каждую минуту)
    scheduler.add_job(send_reminders, "cron", minute="*")
    scheduler.start()

    # Запуск HTTP-сервера для Render (health check)
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"HTTP-сервер запущен на порту {port}")

    # Запуск бота
    logging.info("Бот запущен!")
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
