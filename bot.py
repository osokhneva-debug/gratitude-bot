import asyncio
import logging
import os
import re
import signal
from datetime import datetime, timezone, timedelta
from io import BytesIO
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    BotCommand, BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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
        [KeyboardButton(text="⏰ Настройки"), KeyboardButton(text="ℹ️ О боте")]
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


def extract_mentions(text: str) -> list[str]:
    """Извлекает все @username из текста"""
    pattern = r'@([a-zA-Z][a-zA-Z0-9_]{4,31})'
    return re.findall(pattern, text)


# ==================== ХЕНДЛЕРЫ ====================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Приветствие и онбординг"""
    username = message.from_user.username
    is_new = await db.add_user(message.from_user.id, username)

    if is_new:
        # Новый пользователь — показываем онбординг
        await message.answer(
            "🙏 Привет! Я — твой Дневник Благодарностей.\n\n"
            "Практика благодарности помогает замечать хорошее "
            "в жизни и чувствовать себя счастливее.\n\n"
            "Каждый день я буду напоминать тебе записать, "
            "за что ты благодарен. Это займёт пару минут.\n\n"
            "Этот бот создан Ольгой Сохневой — автором канала "
            "«<a href='https://t.me/remote_love_2'>Любовь на удаленке</a>». "
            "Буду рада твоей подписке на канал — там эксперименты с ИИ, "
            "карьерой и привычками, которые вдохновляют пробовать новое "
            "и выстраивать жизнь под себя.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        # Проверяем отложенные благодарности для нового пользователя
        if username:
            await deliver_pending_gratitudes(message.from_user.id, username)

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

        # Проверяем отложенные благодарности
        if username:
            await deliver_pending_gratitudes(message.from_user.id, username)


async def ask_timezone(message: Message, state: FSMContext):
    """Запрос текущего времени для определения часового пояса"""
    await state.set_state(GratitudeStates.waiting_for_current_time)

    # Кнопка отмены
    cancel_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

    await message.answer(
        "🕐 Сколько сейчас у тебя времени?\n\n"
        "Напиши в формате ЧЧ:ММ, например: 14:30",
        reply_markup=cancel_keyboard
    )


@dp.message(GratitudeStates.waiting_for_current_time)
async def process_current_time(message: Message, state: FSMContext):
    """Обработка ввода текущего времени для расчёта часового пояса"""
    # Обработка отмены
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Действие отменено.",
            reply_markup=main_menu
        )
        return

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
            f"Я буду напоминать тебе в 21:00 по твоему времени.\n"
            f"Изменить время можно командой /time\n\n"
            f"Также по кнопке «Записать» ты можешь сохранять слова благодарности в любое время."
        )

        await state.clear()
        await asyncio.sleep(1)

        await message.answer(
            "Теперь ты готов начать!\n\n"
            "Нажми 📝 Записать, чтобы добавить первую запись. "
            "Ты можешь это сделать в любое время суток или бот напомнит тебе "
            "в то время, которое ты выбрал.",
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
    # Проверяем отложенные благодарности
    username = message.from_user.username
    if username:
        await deliver_pending_gratitudes(message.from_user.id, username)

    await state.set_state(GratitudeStates.waiting_for_gratitudes)
    await state.update_data(gratitudes=[])
    await message.answer(
        "✨ За что ты благодарен сегодня?\n\n"
        "Напиши списком, а если хочешь поблагодарить кого-то — упомяни @username",
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


@dp.callback_query(F.data == "save_gratitudes")
async def save_gratitudes_inline(callback: CallbackQuery, state: FSMContext):
    """Показать финальную карточку (данные уже сохранены автоматически)"""
    # Получаем данные для отображения
    all_today = await db.get_today_entry(callback.from_user.id)

    if not all_today:
        await callback.answer("Сначала напиши хотя бы одну благодарность!", show_alert=True)
        return

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

    total = len(all_today)

    # Убираем inline-кнопки из предыдущего сообщения
    await callback.message.edit_reply_markup(reply_markup=None)

    # Основное сообщение с результатом
    await callback.message.answer(
        f"🎉 Готово! ({total} благодарностей){congrats}\n\n{card}",
        reply_markup=main_menu
    )

    await callback.answer()


@dp.callback_query(F.data == "cancel_gratitudes")
async def cancel_gratitudes_inline(callback: CallbackQuery, state: FSMContext):
    """Закрыть режим записи (данные уже сохранены)"""
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Режим записи закрыт. Твои благодарности сохранены!", reply_markup=main_menu)
    await callback.answer()


@dp.message(GratitudeStates.waiting_for_gratitudes)
async def process_gratitude(message: Message, state: FSMContext):
    """Обработка ввода благодарностей с автосохранением"""
    # Игнорируем команды — они не должны добавляться как благодарности
    if message.text and message.text.startswith('/'):
        return

    # Разбиваем на строки, если пользователь прислал список
    new_items = [line.strip() for line in message.text.split("\n") if line.strip()]

    if not new_items:
        return

    # Сразу сохраняем в базу (автосохранение)
    await db.save_entry(message.from_user.id, new_items)

    # Обрабатываем упоминания и отправляем уведомления
    mention_status = await process_gratitude_mentions(message.from_user.id, new_items)

    # Получаем общее количество за сегодня для отображения счетчика
    all_today = await db.get_today_entry(message.from_user.id)
    total = len(all_today)

    # Inline-кнопки для финального просмотра/отмены
    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💾 Готово", callback_data="save_gratitudes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_gratitudes")
        ]
    ])

    # Короткое подтверждение сохранения
    await message.answer(
        f"✅ Сохранено (+{len(new_items)}), всего за день: {total}\n\n"
        "Продолжай писать или нажми Готово для просмотра.",
        reply_markup=inline_kb
    )

    # Если есть pending упоминания — показываем отдельным сообщением
    if mention_status["pending"]:
        pending_users = ", ".join([f"@{u}" for u in mention_status["pending"]])
        await message.answer(
            f"💌 {pending_users} получит твою благодарность, когда присоединится к боту\n\n"
            f"Пригласить: https://t.me/thanksworld_bot"
        )


@dp.message(Command("diary"))
@dp.message(F.text == "📖 Дневник")
async def cmd_diary(message: Message):
    """Показать архив записей со статистикой"""
    # Проверяем отложенные благодарности
    username = message.from_user.username
    if username:
        await deliver_pending_gratitudes(message.from_user.id, username)

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

    # Кнопка экспорта PDF
    export_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Скачать PDF", callback_data="export_pdf")]
    ])

    await message.answer(
        f"{stats_header}{throwback_text}\n─────────────────",
        parse_mode="HTML",
        reply_markup=export_kb
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
        "<b>💡 Благодарности для других:</b>\n"
        "Упомяни @username в своей записи, чтобы человек получил уведомление. "
        "Если у него еще нет бота — благодарность дойдет, когда он присоединится!\n\n"
        "Бот будет напоминать тебе каждый день в выбранное время.",
        parse_mode="HTML",
        reply_markup=main_menu
    )


@dp.message(Command("about"))
@dp.message(F.text == "ℹ️ О боте")
async def cmd_about(message: Message):
    """Информация о боте и подписка на канал"""
    about_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url="https://t.me/remote_love_2")]
    ])

    await message.answer(
        "🙏 <b>Дневник Благодарностей</b>\n\n"
        "Бот помогает развивать привычку замечать хорошее в жизни.\n\n"
        "Практика благодарности — это простой способ стать счастливее. "
        "Исследования показывают, что люди, которые регулярно записывают "
        "благодарности, чувствуют себя лучше и оптимистичнее.\n\n"
        "Подписывайся на канал «Любовь на удаленке | Оля Сохнева» — там эксперименты с ИИ, "
        "карьерой и привычками, которые вдохновляют пробовать новое и настраивать жизнь под себя.",
        parse_mode="HTML",
        reply_markup=about_keyboard
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

    # Кнопка отмены
    cancel_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

    await callback.message.answer(
        "🕐 Сколько сейчас у тебя времени?\n\n"
        "Напиши в формате ЧЧ:ММ, например: 14:30",
        reply_markup=cancel_keyboard
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


@dp.callback_query(F.data.startswith("thank_back_"))
async def thank_back(callback: CallbackQuery, state: FSMContext):
    """Поблагодарить в ответ"""
    # Извлекаем user_id того, кому отвечаем
    target_user_id = int(callback.data.split("_")[2])
    target_username = await db.get_username_by_id(target_user_id)

    # Сохраняем в state для использования при сохранении
    await state.update_data(
        gratitudes=[],
        thank_back_to=target_user_id,
        thank_back_username=target_username
    )
    await state.set_state(GratitudeStates.waiting_for_gratitudes)

    target_name = f"@{target_username}" if target_username else "этого человека"

    await callback.message.answer(
        f"📝 Напиши благодарность для {target_name}:\n\n"
        f"Можешь упомянуть {target_name} в тексте, чтобы они получили уведомление.",
        reply_markup=write_keyboard
    )
    await callback.answer()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

async def deliver_pending_gratitudes(user_id: int, username: str):
    """Доставляет отложенные благодарности пользователю"""
    pending = await db.get_pending_gratitudes(username)

    for gratitude in pending:
        try:
            reply_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📝 Поблагодарить в ответ",
                    callback_data=f"thank_back_{gratitude['from_user_id']}"
                )]
            ])

            sender_name = f"@{gratitude['from_username']}" if gratitude['from_username'] else "Кто-то"
            date_str = gratitude['date'].strftime("%d.%m.%Y")

            await bot.send_message(
                user_id,
                f"🙏 <b>{sender_name} поблагодарил тебя ({date_str}):</b>\n\n"
                f"«{gratitude['text']}»",
                parse_mode="HTML",
                reply_markup=reply_kb
            )

            # Отмечаем как доставленную
            await db.mark_gratitude_delivered(gratitude['id'])
            logging.info(f"Delivered pending gratitude {gratitude['id']} to {user_id}")

            await asyncio.sleep(0.5)  # Пауза между сообщениями
        except Exception as e:
            logging.error(f"Failed to deliver pending gratitude: {e}")


async def process_gratitude_mentions(from_user_id: int, gratitudes: list[str]) -> dict:
    """Обрабатывает упоминания в благодарностях и отправляет уведомления

    Возвращает:
        dict: {"delivered": [username, ...], "pending": [username, ...]}
    """
    from_username = await db.get_username_by_id(from_user_id)
    delivered = []
    pending = []

    for text in gratitudes:
        mentions = extract_mentions(text)

        for mention in mentions:
            # Ищем пользователя по username
            to_user_id = await db.get_user_by_username(mention)

            if to_user_id:
                # Пользователь в боте — отправляем уведомление
                try:
                    reply_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="📝 Поблагодарить в ответ",
                            callback_data=f"thank_back_{from_user_id}"
                        )]
                    ])

                    sender_name = f"@{from_username}" if from_username else "Кто-то"

                    await bot.send_message(
                        to_user_id,
                        f"🙏 <b>{sender_name} поблагодарил тебя:</b>\n\n"
                        f"«{text}»",
                        parse_mode="HTML",
                        reply_markup=reply_kb
                    )
                    delivered.append(mention)
                    logging.info(f"Sent gratitude notification from {from_user_id} to {to_user_id}")
                except Exception as e:
                    # Не удалось отправить (пользователь еще не писал боту или заблокировал его)
                    logging.error(f"Failed to send gratitude notification to @{mention} (user_id={to_user_id}): {e}")
                    # Сохраняем как pending, чтобы доставить позже
                    await db.save_pending_gratitude(from_user_id, mention, text)
                    pending.append(mention)
                    logging.info(f"Saved as pending gratitude for @{mention} due to delivery failure")
            else:
                # Пользователя нет в боте — сохраняем отложенную благодарность
                await db.save_pending_gratitude(from_user_id, mention, text)
                pending.append(mention)
                logging.info(f"Saved pending gratitude for @{mention}")

    return {"delivered": delivered, "pending": pending}


def generate_pdf(entries: list, streak: int, total_gratitudes: int) -> BytesIO:
    """Генерирует PDF с дневником благодарностей"""
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=2*cm, bottomMargin=2*cm)

    # Регистрируем шрифт с поддержкой кириллицы
    import os
    font_path = os.path.join(os.path.dirname(__file__), 'DejaVuSans.ttf')
    pdfmetrics.registerFont(TTFont('DejaVuSans', font_path))

    # Стили с кириллическим шрифтом
    title_style = ParagraphStyle(
        'Title',
        fontName='DejaVuSans',
        fontSize=24,
        spaceAfter=20,
        alignment=1  # center
    )
    stats_style = ParagraphStyle(
        'Stats',
        fontName='DejaVuSans',
        fontSize=12,
        spaceAfter=10,
        alignment=1
    )
    date_style = ParagraphStyle(
        'Date',
        fontName='DejaVuSans',
        fontSize=14,
        spaceBefore=15,
        spaceAfter=5
    )
    item_style = ParagraphStyle(
        'Item',
        fontName='DejaVuSans',
        fontSize=11,
        leftIndent=20,
        spaceAfter=3
    )

    story = []

    # Заголовок
    story.append(Paragraph("Дневник Благодарностей", title_style))
    story.append(Spacer(1, 10))

    # Статистика
    streak_text = f"Серия: {streak} дней | Записей: {len(entries)} | Благодарностей: {total_gratitudes}"
    story.append(Paragraph(streak_text, stats_style))
    story.append(Spacer(1, 20))

    # Записи по дням
    for entry in reversed(entries):  # От новых к старым
        date = entry["date"]
        if isinstance(date, str):
            date = datetime.fromisoformat(date)
        date_str = date.strftime("%d.%m.%Y")

        story.append(Paragraph(date_str, date_style))

        for i, item in enumerate(entry["gratitudes"], 1):
            # Экранируем HTML-символы и заменяем кириллицу на транслит для совместимости
            safe_item = item.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(f"{i}. {safe_item}", item_style))

        story.append(Spacer(1, 10))

    # Футер со ссылкой на канал
    footer_style = ParagraphStyle(
        'Footer',
        fontName='DejaVuSans',
        fontSize=10,
        spaceBefore=30,
        alignment=1,
        textColor='#666666'
    )
    link_style = ParagraphStyle(
        'Link',
        fontName='DejaVuSans',
        fontSize=10,
        alignment=1,
        textColor='#0066cc'
    )

    story.append(Spacer(1, 30))
    story.append(Paragraph("─" * 40, footer_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Этот бот создан Ольгой Сохневой — автором канала",
        footer_style
    ))
    story.append(Paragraph(
        '<a href="https://t.me/remote_love_2" color="#0066cc">«Любовь на удаленке»</a>',
        link_style
    ))
    story.append(Paragraph(
        "Буду рада твоей подписке на канал — там эксперименты с ИИ,",
        footer_style
    ))
    story.append(Paragraph(
        "карьерой и привычками, которые вдохновляют пробовать новое",
        footer_style
    ))
    story.append(Paragraph(
        "и выстраивать жизнь под себя.",
        footer_style
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer


@dp.callback_query(F.data == "export_pdf")
async def export_diary_pdf(callback: CallbackQuery):
    """Экспорт дневника в PDF"""
    await callback.answer("Генерирую PDF...")

    entries = await db.get_entries(callback.from_user.id)

    if not entries:
        await callback.message.answer("У тебя пока нет записей для экспорта.")
        return

    streak = await db.get_streak(callback.from_user.id)
    total_gratitudes = await db.get_total_gratitudes_count(callback.from_user.id)

    # Генерируем PDF
    pdf_buffer = generate_pdf(entries, streak, total_gratitudes)

    # Отправляем файл
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"gratitude_diary_{date_str}.pdf"

    await callback.message.answer_document(
        BufferedInputFile(pdf_buffer.read(), filename=filename),
        caption="📥 Твой дневник благодарностей"
    )


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
    """Отправка напоминаний с учётом часовых поясов (оптимизировано)"""
    utc_now = datetime.now(timezone.utc)

    # Получаем только тех пользователей, кому нужно отправить сейчас
    users_to_notify = await db.get_users_for_reminder(utc_now.hour, utc_now.minute)

    if users_to_notify:
        logging.info(f"Sending reminders to {len(users_to_notify)} users at UTC {utc_now.hour}:{utc_now.minute:02d}")

    sent_count = 0
    error_count = 0

    for user_id in users_to_notify:
        try:
            # Проверяем есть ли pending благодарности
            username = await db.get_username_by_id(user_id)
            pending_count = 0
            if username:
                pending = await db.get_pending_gratitudes(username)
                pending_count = len(pending)

            # Устанавливаем состояние ожидания благодарностей
            state = FSMContext(storage=dp.storage, key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id))
            await state.set_state(GratitudeStates.waiting_for_gratitudes)
            await state.update_data(gratitudes=[])

            # Inline-кнопки для сохранения/отмены
            inline_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="💾 Сохранить", callback_data="save_gratitudes"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_gratitudes")
                ]
            ])

            # Формируем текст сообщения с подсказкой о pending благодарностях
            reminder_text = "🌙 Привет!\n\n"
            if pending_count > 0:
                reminder_text += f"💌 У тебя {pending_count} {'новая благодарность' if pending_count == 1 else 'новые благодарности' if pending_count < 5 else 'новых благодарностей'}!\n\n"
            reminder_text += "За что ты благодарен сегодня?\n\n"
            reminder_text += "Напиши списком, а если хочешь поблагодарить кого-то — упомяни @username"

            await bot.send_message(
                user_id,
                reminder_text,
                reply_markup=inline_kb
            )
            sent_count += 1
            # Rate limiting: пауза между сообщениями (Telegram limit: 30 msg/sec)
            await asyncio.sleep(0.05)
        except Exception as e:
            error_count += 1
            logging.error(f"Не удалось отправить напоминание {user_id}: {e}")

    if sent_count > 0 or error_count > 0:
        logging.info(f"Reminders: sent={sent_count}, errors={error_count}")


# ==================== ЗАПУСК ====================

# URL для webhook (установи в переменных окружения на Render)
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # например: https://gratitude-bot-8h4i.onrender.com


async def webhook_handler(request):
    """Обработчик входящих обновлений от Telegram"""
    try:
        data = await request.json()
        from aiogram.types import Update
        update = Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logging.error(f"Ошибка обработки webhook: {e}")
    return web.Response(text="OK")


async def health_check(request):
    """Health-check эндпоинт для Render"""
    return web.Response(text="OK")


async def on_startup():
    """Действия при запуске бота"""
    # Удаляем старый webhook и устанавливаем новый
    if WEBHOOK_URL:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
        logging.info(f"Webhook установлен: {WEBHOOK_URL}/webhook")
    else:
        logging.warning("WEBHOOK_URL не установлен! Бот работает в режиме polling (не рекомендуется)")


async def on_shutdown():
    """Действия при остановке бота"""
    logging.info("Завершаем работу...")
    scheduler.shutdown(wait=False)
    if WEBHOOK_URL:
        await bot.delete_webhook()
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

    # Настройка напоминаний (проверяем каждую минуту)
    scheduler.add_job(send_reminders, "cron", minute="*")
    scheduler.start()

    # Создаём веб-приложение
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_post("/webhook", webhook_handler)

    # Регистрируем события запуска/остановки
    app.on_startup.append(lambda _: on_startup())
    app.on_shutdown.append(lambda _: on_shutdown())

    port = int(os.environ.get("PORT", 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"HTTP-сервер запущен на порту {port}")

    # Если webhook не настроен — используем polling (для локальной разработки)
    if WEBHOOK_URL:
        logging.info("Бот запущен в режиме webhook!")
        # Держим приложение запущенным
        while True:
            await asyncio.sleep(3600)
    else:
        logging.info("Бот запущен в режиме polling (локальная разработка)")
        await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
