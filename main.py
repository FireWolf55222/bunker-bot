import asyncio
import logging
import sqlite3
import html
import os
import threading
from datetime import datetime, timedelta
from os import getenv, path

from flask import Flask
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile,
)
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = getenv("BOT_TOKEN")
MANAGER_ID = getenv("MANAGER_ID")
if not BOT_TOKEN or not MANAGER_ID:
    raise ValueError("Не заданы BOT_TOKEN или MANAGER_ID в .env")
MANAGER_ID = int(MANAGER_ID)

logging.basicConfig(level=logging.INFO)

DB_NAME = "bunker_requests.db"

# ---------- Услуги и параметры ----------
SERVICES = {
    "Полировка": {
        "Лёгкая": {"duration": 3, "requires_time": False},
        "Глубокая": {"duration": 48, "requires_time": False}
    },
    "Химчистка": {
        "Без разбора": {"duration": 48, "requires_time": False},
        "С разбором": {"duration": 48, "requires_time": False}
    },
    "Тонировка": {
        "Передняя полусфера": {"duration": 2, "requires_time": True},
        "Задняя полусфера": {"duration": 2.5, "requires_time": True}
    }
}

WORK_START_HOUR = 10
WORK_END_HOUR = 22
SLOT_INTERVAL = 30

def get_service_params(category, subcategory):
    return SERVICES.get(category, {}).get(subcategory, {"duration": 1, "requires_time": False})

def is_long_duration(duration_hours):
    return duration_hours >= 8

# ---------- База данных ----------
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            service_category TEXT,
            service_subcategory TEXT,
            date TEXT,
            time_start TEXT,
            duration_hours REAL,
            car TEXT,
            phone TEXT,
            comment TEXT,
            status TEXT DEFAULT 'новая',
            created_at TEXT,
            reminder_24h_sent BOOLEAN DEFAULT 0,
            reminder_1h_sent BOOLEAN DEFAULT 0
        )
    """)

    cur.execute("PRAGMA table_info(requests)")
    existing_columns = [col[1] for col in cur.fetchall()]

    if "reminder_24h_sent" not in existing_columns:
        cur.execute("ALTER TABLE requests ADD COLUMN reminder_24h_sent BOOLEAN DEFAULT 0")

    if "reminder_1h_sent" not in existing_columns:
        cur.execute("ALTER TABLE requests ADD COLUMN reminder_1h_sent BOOLEAN DEFAULT 0")

    conn.commit()
    conn.close()

def save_request(user_id, username, category, subcategory, date_str, time_start, duration, car, phone, comment):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO requests (
            user_id, username, service_category, service_subcategory,
            date, time_start, duration_hours, car, phone, comment, status, created_at,
            reminder_24h_sent, reminder_1h_sent
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'новая', ?, 0, 0)
    """, (user_id, username, category, subcategory, date_str, time_start, duration,
          car, phone, comment, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return cur.lastrowid

def get_requests(limit=5, offset=0, status_filter=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    query = """
        SELECT id, user_id, username, service_category, service_subcategory,
               date, time_start, duration_hours, car, phone, comment, status, created_at,
               reminder_24h_sent, reminder_1h_sent
        FROM requests
    """
    params = []
    if status_filter and status_filter != 'все':
        query += " WHERE status = ?"
        params.append(status_filter)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows

def count_requests(status_filter=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    query = "SELECT COUNT(*) FROM requests"
    params = []
    if status_filter and status_filter != 'все':
        query += " WHERE status = ?"
        params.append(status_filter)
    cur.execute(query, params)
    count = cur.fetchone()[0]
    conn.close()
    return count

def get_request_by_id(req_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_request_status(req_id, new_status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE requests SET status = ? WHERE id = ?", (new_status, req_id))
    conn.commit()
    conn.close()

def delete_request(req_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM requests WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM requests")
    total = cur.fetchone()[0]
    cur.execute("SELECT service_category, COUNT(*) FROM requests GROUP BY service_category")
    by_category = cur.fetchall()
    cur.execute("SELECT status, COUNT(*) FROM requests GROUP BY status")
    by_status = cur.fetchall()
    conn.close()
    return total, by_category, by_status

def get_all_active_requests():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT id, user_id, username, date, time_start, service_category, service_subcategory,
               reminder_24h_sent, reminder_1h_sent
        FROM requests
        WHERE status NOT IN ('отменена', 'выполнена')
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def mark_reminder_sent(req_id, reminder_type):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    if reminder_type == '24h':
        cur.execute("UPDATE requests SET reminder_24h_sent = 1 WHERE id = ?", (req_id,))
    elif reminder_type == '1h':
        cur.execute("UPDATE requests SET reminder_1h_sent = 1 WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()

# ---------- Проверка доступности ----------
def is_date_available(date_str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT duration_hours FROM requests
        WHERE date = ? AND status NOT IN ('отменена', 'выполнена')
    """, (date_str,))
    rows = cur.fetchall()
    conn.close()
    for (dur,) in rows:
        if dur >= 8:
            return False
    return True

def get_busy_time_slots(date_str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        SELECT time_start, duration_hours FROM requests
        WHERE date = ? AND time_start IS NOT NULL
          AND status NOT IN ('отменена', 'выполнена')
    """, (date_str,))
    rows = cur.fetchall()
    conn.close()
    intervals = []
    for time_str, dur in rows:
        h, m = map(int, time_str.split(':'))
        start_min = h * 60 + m
        end_min = start_min + int(dur * 60)
        intervals.append((start_min, end_min))
    return intervals

def get_available_time_slots(date_str, duration_hours):
    if not is_date_available(date_str):
        return []
    busy = get_busy_time_slots(date_str)
    step = SLOT_INTERVAL
    start_min = WORK_START_HOUR * 60
    end_min = WORK_END_HOUR * 60
    duration_min = int(duration_hours * 60)
    slots = []
    t = start_min
    while t + duration_min <= end_min:
        free = True
        for b_start, b_end in busy:
            if not (t + duration_min <= b_start or t >= b_end):
                free = False
                break
        if free:
            h = t // 60
            m = t % 60
            slots.append(f"{h:02d}:{m:02d}")
        t += step
    return slots

# ---------- Функция отправки напоминаний ----------
async def send_reminders(bot: Bot):
    now = datetime.now()
    requests = get_all_active_requests()
    for req in requests:
        req_id, user_id, username, date_str, time_start, category, subcategory, rem24, rem1 = req
        try:
            dt_date = datetime.strptime(date_str, "%d.%m.%Y")
        except:
            continue

        if time_start:
            try:
                h, m = map(int, time_start.split(':'))
                dt_appointment = dt_date.replace(hour=h, minute=m, second=0, microsecond=0)
            except:
                continue
        else:
            dt_appointment = dt_date.replace(hour=WORK_START_HOUR, minute=0, second=0, microsecond=0)

        if not rem24:
            delta = dt_appointment - now
            if timedelta(hours=23.5) <= delta <= timedelta(hours=24.5):
                text = (
                    f"📅 <b>Напоминание!</b>\n"
                    f"Вы записаны в BUNKER Detailing на завтра.\n"
                    f"🛠 {category} → {subcategory}\n"
                    f"📅 {date_str}"
                )
                if time_start:
                    text += f"\n🕒 {time_start}"
                text += "\n\nЖдём вас! 🚗"
                try:
                    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                    mark_reminder_sent(req_id, '24h')
                    logging.info(f"Отправлено напоминание за 24ч для заявки {req_id}")
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания 24ч для {req_id}: {e}")

        if time_start and not rem1:
            delta = dt_appointment - now
            if timedelta(minutes=55) <= delta <= timedelta(minutes=65):
                text = (
                    f"⏰ <b>Напоминание!</b>\n"
                    f"Вы записаны в BUNKER Detailing через 1 час.\n"
                    f"🛠 {category} → {subcategory}\n"
                    f"📅 {date_str} в {time_start}\n\n"
                    "Не опаздывайте! 🚗"
                )
                try:
                    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                    mark_reminder_sent(req_id, '1h')
                    logging.info(f"Отправлено напоминание за 1ч для заявки {req_id}")
                except Exception as e:
                    logging.error(f"Ошибка отправки напоминания 1ч для {req_id}: {e}")

# ---------- FSM ----------
class Booking(StatesGroup):
    waiting_service_category = State()
    waiting_service_subcategory = State()
    waiting_other_service = State()
    waiting_date = State()
    waiting_time = State()
    waiting_car = State()
    waiting_phone = State()
    waiting_comment = State()
    waiting_confirm = State()

# ---------- Клавиатуры ----------
skip_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Пропустить")]],
    resize_keyboard=True,
    one_time_keyboard=True
)
confirm_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✅ Да, всё верно"), KeyboardButton(text="❌ Нет, исправить")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)

# ---------- Бот ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ---------- Вспомогательные функции для админки ----------
def format_request_row(row):
    req_id, user_id, username, category, subcategory, date_str, time_start, duration, car, phone, comment, status, created_at, rem24, rem1 = row
    status_emoji = {
        'новая': '🟢',
        'подтверждена': '🟡',
        'выполнена': '🟣',
        'отменена': '🔴'
    }.get(status, '⚪')
    date_short = datetime.fromisoformat(created_at).strftime("%d.%m %H:%M")
    time_display = f" {time_start}" if time_start else ""
    return f"{status_emoji} #{req_id} {subcategory}{time_display} — {car} ({date_short})"

def get_status_kb(req_id):
    buttons = [
        [InlineKeyboardButton(text="🟢 Новая", callback_data=f"setstatus_{req_id}_новая"),
         InlineKeyboardButton(text="🟡 Подтверждена", callback_data=f"setstatus_{req_id}_подтверждена")],
        [InlineKeyboardButton(text="🟣 Выполнена", callback_data=f"setstatus_{req_id}_выполнена"),
         InlineKeyboardButton(text="🔴 Отменена", callback_data=f"setstatus_{req_id}_отменена")],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{req_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- Обработчики ----------
@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    photo_path = "welcome.jpg"
    caption = (
        "🚗 <b>Добро пожаловать в BUNKER Detailing!</b>\n\n"
        "Выберите услугу:\n"
        "• 🧽 Полировка\n"
        "• 🧹 Химчистка\n"
        "• 🪟 Тонировка\n\n"
        "📍 <b>Адрес:</b> ул. Автомобильная, 123\n"
        "📞 <b>Телефон:</b> <a href='tel:+71234567890'>+7 (123) 456-78-90</a>\n"
        "📸 <b>Instagram:</b> @bunker_detailing\n\n"
        "👇 Нажмите, чтобы записаться"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Записаться", callback_data="start_booking")]
    ])
    if path.exists(photo_path):
        photo = FSInputFile(photo_path)
        await message.answer_photo(photo, caption=caption, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(caption, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "start_booking")
async def start_booking(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧽 Полировка", callback_data="cat_Полировка")],
        [InlineKeyboardButton(text="🧹 Химчистка", callback_data="cat_Химчистка")],
        [InlineKeyboardButton(text="🪟 Тонировка", callback_data="cat_Тонировка")],
        [InlineKeyboardButton(text="✏️ Другое", callback_data="cat_Другое")]
    ])
    await callback.message.answer(
        "🛠 <b>Выберите категорию услуги</b>:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await state.set_state(Booking.waiting_service_category)

@router.callback_query(Booking.waiting_service_category)
async def process_category(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    cat = callback.data.replace("cat_", "")
    if cat == "Другое":
        await callback.message.edit_text(
            "✏️ Напишите, какая услуга вас интересует:",
            reply_markup=None
        )
        await state.set_state(Booking.waiting_other_service)
        return
    await state.update_data(category=cat)
    if cat in SERVICES and SERVICES[cat]:
        subcats = list(SERVICES[cat].keys())
        kb_buttons = []
        for sub in subcats:
            kb_buttons.append([InlineKeyboardButton(text=sub, callback_data=f"sub_{sub}")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        await callback.message.edit_text(
            f"🧩 <b>Выберите тип {cat.lower()}:</b>",
            reply_markup=kb,
            parse_mode="HTML"
        )
        await state.set_state(Booking.waiting_service_subcategory)
    else:
        params = {"duration": 1, "requires_time": False}
        await state.update_data(subcategory=cat, duration=params["duration"], requires_time=params["requires_time"])
        await show_calendar(callback.message, state)

@router.callback_query(Booking.waiting_service_subcategory)
async def process_subcategory(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    sub = callback.data.replace("sub_", "")
    data = await state.get_data()
    cat = data.get('category')
    params = get_service_params(cat, sub)
    await state.update_data(subcategory=sub, duration=params["duration"], requires_time=params["requires_time"])
    await show_calendar(callback.message, state)

@router.message(Booking.waiting_other_service)
async def process_other_service(message: types.Message, state: FSMContext):
    if not message.text.strip():
        await message.answer("Пожалуйста, напишите название услуги.")
        return
    await state.update_data(category="Другое", subcategory=message.text.strip(), duration=2, requires_time=False)
    await show_calendar(message, state)

async def show_calendar(message: types.Message, state: FSMContext):
    now = datetime.now()
    calendar = SimpleCalendar(locale='ru')
    await message.answer(
        "📅 <b>Выберите дату</b> (дни, занятые длительными услугами, будут заблокированы):",
        reply_markup=await calendar.start_calendar(year=now.year, month=now.month),
        parse_mode="HTML"
    )
    await state.set_state(Booking.waiting_date)

@router.callback_query(SimpleCalendarCallback.filter())
async def process_calendar(callback: CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    calendar = SimpleCalendar(locale='ru')
    selected, date_obj = await calendar.process_selection(callback, callback_data)
    if selected:
        date_str = date_obj.strftime("%d.%m.%Y")
        if not is_date_available(date_str):
            await callback.message.edit_text(
                "❌ Эта дата занята длительной услугой (химчистка, глубокая полировка). Пожалуйста, выберите другую дату.",
                reply_markup=await calendar.start_calendar(year=date_obj.year, month=date_obj.month)
            )
            return
        await state.update_data(date=date_str)
        data = await state.get_data()
        if data.get('requires_time', False):
            duration = data.get('duration')
            slots = get_available_time_slots(date_str, duration)
            if not slots:
                await callback.message.edit_text(
                    "❌ На эту дату нет свободных временных слотов. Выберите другую дату.",
                    reply_markup=await calendar.start_calendar(year=date_obj.year, month=date_obj.month)
                )
                return
            await state.update_data(available_slots=slots)
            kb_buttons = []
            row = []
            for slot in slots:
                row.append(InlineKeyboardButton(text=slot, callback_data=f"time_{slot}"))
                if len(row) == 3:
                    kb_buttons.append(row)
                    row = []
            if row:
                kb_buttons.append(row)
            kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
            await callback.message.edit_text(
                "🕒 <b>Выберите удобное время начала:</b>",
                reply_markup=kb,
                parse_mode="HTML"
            )
            await state.set_state(Booking.waiting_time)
        else:
            await callback.message.edit_text(
                f"✅ Выбрана дата: <b>{date_str}</b>\n"
                "Теперь укажите марку и модель автомобиля:",
                parse_mode="HTML"
            )
            await state.set_state(Booking.waiting_car)

@router.callback_query(Booking.waiting_time)
async def process_time_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    time_str = callback.data.replace("time_", "")
    data = await state.get_data()
    date_str = data.get('date')
    duration = data.get('duration')
    slots = get_available_time_slots(date_str, duration)
    if time_str not in slots:
        await callback.message.edit_text(
            "❌ Этот слот уже занят. Пожалуйста, выберите другой."
        )
        new_slots = get_available_time_slots(date_str, duration)
        if new_slots:
            await state.update_data(available_slots=new_slots)
            kb_buttons = []
            row = []
            for slot in new_slots:
                row.append(InlineKeyboardButton(text=slot, callback_data=f"time_{slot}"))
                if len(row) == 3:
                    kb_buttons.append(row)
                    row = []
            if row:
                kb_buttons.append(row)
            kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
            await callback.message.edit_text(
                "🕒 <b>Выберите удобное время начала:</b>",
                reply_markup=kb,
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "❌ На эту дату больше нет свободных слотов. Попробуйте выбрать другую дату через /start"
            )
            await state.clear()
        return
    await state.update_data(time_start=time_str)
    await callback.message.edit_text(
        f"✅ Выбрано время: <b>{time_str}</b>\n"
        "Теперь укажите марку и модель автомобиля:",
        parse_mode="HTML"
    )
    await state.set_state(Booking.waiting_car)

@router.message(Booking.waiting_car)
async def process_car(message: types.Message, state: FSMContext):
    car = message.text.strip()
    if not car:
        await message.answer("Пожалуйста, введите марку и модель.")
        return
    await state.update_data(car=car)
    await message.answer(
        "📞 <b>Укажите ваш контактный телефон</b>\n"
        "Например: +7 (123) 456-78-90",
        parse_mode="HTML"
    )
    await state.set_state(Booking.waiting_phone)

@router.message(Booking.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not phone:
        await message.answer("Пожалуйста, укажите телефон для связи.")
        return
    if not any(ch.isdigit() for ch in phone):
        await message.answer("❌ Номер должен содержать цифры. Попробуйте ещё раз.")
        return
    await state.update_data(phone=phone)
    await message.answer(
        "💬 <b>Комментарий к заявке</b> (необязательно)\n"
        "Если есть особые пожелания, напишите их. Или нажмите «Пропустить».",
        reply_markup=skip_kb,
        parse_mode="HTML"
    )
    await state.set_state(Booking.waiting_comment)

@router.message(Booking.waiting_comment)
async def process_comment(message: types.Message, state: FSMContext):
    comment = message.text.strip()
    if comment == "Пропустить":
        comment = "Без комментария"
    await state.update_data(comment=comment)
    data = await state.get_data()
    date_str = data.get('date')
    if not is_date_available(date_str):
        await message.answer(
            "❌ К сожалению, эта дата уже занята длительной услугой. Пожалуйста, начните заново с /start."
        )
        await state.clear()
        return
    if data.get('requires_time', False):
        time_str = data.get('time_start')
        duration = data.get('duration')
        slots = get_available_time_slots(date_str, duration)
        if time_str not in slots:
            await message.answer(
                "❌ К сожалению, выбранный слот уже занят. Пожалуйста, начните заново с /start."
            )
            await state.clear()
            return

    text = (
        "✅ <b>Проверьте введённые данные:</b>\n\n"
        f"🛠 Услуга: <b>{html.escape(data.get('category'))} → {html.escape(data.get('subcategory'))}</b>\n"
        f"📅 Дата: <b>{html.escape(date_str)}</b>\n"
    )
    if data.get('requires_time', False):
        text += f"🕒 Время: <b>{html.escape(data.get('time_start'))}</b>\n"
    text += (
        f"⏳ Длительность: <b>{data.get('duration')} ч.</b>\n"
        f"🚘 Авто: <b>{html.escape(data.get('car'))}</b>\n"
        f"📞 Телефон: <b>{html.escape(data.get('phone'))}</b>\n"
        f"💬 Комментарий: <b>{html.escape(data.get('comment'))}</b>\n\n"
        "Всё верно? Нажмите «Да» или «Нет»."
    )
    await message.answer(text, reply_markup=confirm_kb, parse_mode="HTML")
    await state.set_state(Booking.waiting_confirm)

@router.message(Booking.waiting_confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    if message.text == "✅ Да, всё верно":
        data = await state.get_data()
        date_str = data.get('date')
        if not is_date_available(date_str):
            await message.answer(
                "❌ К сожалению, эта дата уже занята. Пожалуйста, начните заново с /start."
            )
            await state.clear()
            return
        if data.get('requires_time', False):
            time_str = data.get('time_start')
            duration = data.get('duration')
            slots = get_available_time_slots(date_str, duration)
            if time_str not in slots:
                await message.answer(
                    "❌ К сожалению, выбранный слот уже занят. Пожалуйста, начните заново с /start."
                )
                await state.clear()
                return
            time_to_save = time_str
        else:
            time_to_save = None

        user_id = message.from_user.id
        username = message.from_user.username or "Не указан"
        req_id = save_request(
            user_id,
            username,
            data.get('category'),
            data.get('subcategory'),
            date_str,
            time_to_save,
            data.get('duration'),
            data.get('car'),
            data.get('phone'),
            data.get('comment')
        )
        report = (
            f"🚗 <b>Новая заявка # {req_id}</b>\n"
            f"🛠 Услуга: {html.escape(data.get('category'))} → {html.escape(data.get('subcategory'))}\n"
            f"📅 Дата: {html.escape(date_str)}\n"
        )
        if time_to_save:
            report += f"🕒 Время: {html.escape(time_to_save)}\n"
        report += (
            f"⏳ Длительность: {data.get('duration')} ч.\n"
            f"🚘 Авто: {html.escape(data.get('car'))}\n"
            f"📞 Телефон: {html.escape(data.get('phone'))}\n"
            f"💬 Комментарий: {html.escape(data.get('comment'))}\n"
            f"👤 От: @{html.escape(username)} (ID: {user_id})"
        )
        try:
            await bot.send_message(chat_id=MANAGER_ID, text=report, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Не удалось отправить менеджеру: {e}")
            await message.answer(
                "⚠️ Произошла ошибка при отправке. Мы уже работаем над этим.\n"
                "Попробуйте позже или свяжитесь с нами по телефону.",
                reply_markup=ReplyKeyboardRemove()
            )
            await state.clear()
            return
        await message.answer(
            "✅ <b>Спасибо!</b> Ваша заявка принята.\n"
            f"Мы забронировали за вами <b>{data.get('duration')} ч.</b> на {html.escape(date_str)}.\n"
            "Наш менеджер свяжется с вами для подтверждения. 📞\n\n"
            "Оставайтесь на связи! 🚗",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
        await state.clear()
    elif message.text == "❌ Нет, исправить":
        await message.answer(
            "Хорошо, давайте начнём заново. Нажмите /start.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.clear()
    else:
        await message.answer(
            "Пожалуйста, нажмите одну из кнопок: «✅ Да» или «❌ Нет».",
            reply_markup=confirm_kb
        )

# ---------- Админ-панель ----------
@router.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != MANAGER_ID:
        await message.answer("⛔ У вас нет доступа.")
        return
    await show_main_menu(message)

async def show_main_menu(message: types.Message, edit=False):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список заявок", callback_data="list_all")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="🔍 Фильтр по статусу", callback_data="filter_menu")]
    ])
    text = "🏢 <b>Админ-панель BUNKER</b>\nВыберите действие:"
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "admin_main")
async def back_to_main(callback: CallbackQuery):
    await callback.answer()
    await show_main_menu(callback.message, edit=True)

@router.callback_query(lambda c: c.data == "list_all")
async def list_requests(callback: CallbackQuery):
    await callback.answer()
    await show_requests_page(callback.message, status_filter="все", page=0)

@router.callback_query(lambda c: c.data.startswith("list_"))
async def list_with_filter(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    status = parts[1] if len(parts) > 1 else "все"
    page = int(parts[2]) if len(parts) > 2 else 0
    await show_requests_page(callback.message, status_filter=status, page=page)

async def show_requests_page(message, status_filter="все", page=0):
    per_page = 5
    offset = page * per_page
    rows = get_requests(limit=per_page, offset=offset, status_filter=status_filter)
    total = count_requests(status_filter)
    total_pages = (total + per_page - 1) // per_page

    if not rows:
        text = "📭 Заявок с таким фильтром нет."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_main")]
        ])
        await message.edit_text(text, reply_markup=kb)
        return

    lines = [f"📋 <b>Заявки</b> (фильтр: {status_filter}) — стр. {page+1}/{total_pages}"]
    for row in rows:
        lines.append(format_request_row(row))
    text = "\n".join(lines)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"list_{status_filter}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"list_{status_filter}_{page+1}"))
    kb_buttons = [nav_buttons] if nav_buttons else []

    for row in rows:
        req_id = row[0]
        kb_buttons.append([InlineKeyboardButton(text=f"Детали #{req_id}", callback_data=f"detail_{req_id}")])

    kb_buttons.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main")])
    kb_buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"list_{status_filter}_{page}")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data.startswith("detail_"))
async def show_detail(callback: CallbackQuery):
    req_id = int(callback.data.split("_")[1])
    row = get_request_by_id(req_id)
    if not row:
        await callback.answer("Заявка не найдена")
        return
    await callback.answer()
    time_display = f"🕒 Время: {row[6]}\n" if row[6] else ""
    text = (
        f"📄 <b>Заявка #{row[0]}</b>\n"
        f"👤 Клиент: @{html.escape(row[2] or 'без username')} (ID: {row[1]})\n"
        f"🛠 Услуга: {html.escape(row[3])} → {html.escape(row[4])}\n"
        f"📅 Дата: {html.escape(row[5])}\n"
        f"{time_display}"
        f"⏳ Длительность: {row[7]} ч.\n"
        f"🚘 Авто: {html.escape(row[8])}\n"
        f"📞 Телефон: <a href='tel:{html.escape(row[9])}'>{html.escape(row[9])}</a>\n"
        f"💬 Комментарий: {html.escape(row[10])}\n"
        f"📊 Статус: {row[11]}\n"
        f"📅 Дата создания: {datetime.fromisoformat(row[12]).strftime('%d.%m.%Y %H:%M')}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="list_all")],
        *get_status_kb(req_id).inline_keyboard
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data.startswith("setstatus_"))
async def set_status(callback: CallbackQuery):
    _, req_id, new_status = callback.data.split("_")
    req_id = int(req_id)
    update_request_status(req_id, new_status)
    await callback.answer(f"Статус изменён на «{new_status}»")
    await show_detail(callback)

@router.callback_query(lambda c: c.data.startswith("delete_"))
async def delete_req(callback: CallbackQuery):
    req_id = int(callback.data.split("_")[1])
    delete_request(req_id)
    await callback.answer("Заявка удалена")
    await show_requests_page(callback.message, status_filter="все", page=0)

@router.callback_query(lambda c: c.data == "filter_menu")
async def filter_menu(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Все", callback_data="list_все_0")],
        [InlineKeyboardButton(text="🟢 Новые", callback_data="list_новая_0")],
        [InlineKeyboardButton(text="🟡 Подтверждены", callback_data="list_подтверждена_0")],
        [InlineKeyboardButton(text="🟣 Выполнены", callback_data="list_выполнена_0")],
        [InlineKeyboardButton(text="🔴 Отменены", callback_data="list_отменена_0")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_main")]
    ])
    await callback.message.edit_text("🔍 <b>Выберите статус для фильтрации:</b>", reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data == "stats")
async def show_stats(callback: CallbackQuery):
    await callback.answer()
    total, by_category, by_status = get_stats()
    lines = ["📊 <b>Статистика заявок BUNKER</b>"]
    lines.append(f"Всего: {total}")
    lines.append("\nПо категориям:")
    for cat, cnt in by_category:
        lines.append(f"  {html.escape(cat)}: {cnt}")
    lines.append("\nПо статусам:")
    for s, cnt in by_status:
        emoji = {'новая':'🟢','подтверждена':'🟡','выполнена':'🟣','отменена':'🔴'}.get(s,'⚪')
        lines.append(f"  {emoji} {s}: {cnt}")
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="admin_main")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")

# ---------- Flask приложение ----------
app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Бот BUNKER работает!"

@app.route('/health')
def health():
    return "OK", 200

# ---------- Запуск ----------
async def run_bot():
    init_db()
    logging.info("Бот BUNKER запущен")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        send_reminders,
        trigger=IntervalTrigger(seconds=30),
        args=[bot],
        id="reminder_job",
        replace_existing=True
    )
    scheduler.start()
    logging.info("Планировщик напоминаний запущен")
    await dp.start_polling(bot)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Запускаем бота в основном потоке
    asyncio.run(run_bot())
