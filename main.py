# -*- coding: utf-8 -*-
"""
Бот-визитка Stryx Automation.
Стек: aiogram 3.x, FSM, SQLite, Pillow, aiohttp (webhook для Render).
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------- Pillow для автогенерации заглушек ----------
try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    PILLOW_OK = True
except ImportError:
    PILLOW_OK = False

# ---------- aiohttp для webhook-сервера ----------
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application


# ---------- Загрузка переменных окружения ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PERSONAL_LINK = os.getenv("PERSONAL_LINK", "https://t.me/your_username")
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")


# ---------- Логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("stryx-bot")


# ---------- Инициализация бота и диспетчера ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ---------- Пути к изображениям ----------
IMG_WELCOME = "images/welcome.jpg"
IMG_ABOUT   = "images/about.jpg"
IMG_CASE_1  = "images/case_1.jpg"
IMG_CASE_2  = "images/case_2.jpg"
IMG_CASE_3  = "images/case_3.jpg"


# ============================================================
# Метаданные изображений (для автозаглушек)
# ============================================================
IMAGE_META = {
    IMG_WELCOME: dict(
        title="Stryx",
        subtitle="AI-автоматизация продаж · 24/7",
        chips=["Telegram-боты", "AI-агенты", "CRM", "Дашборды"],
    ),
    IMG_ABOUT: dict(
        title="Stryx Automation",
        subtitle="Студия автоматизации · 30+ проектов",
        badge="ABOUT",
        chips=["aiogram", "n8n", "YandexGPT", "GigaChat", "CRM"],
    ),
    IMG_CASE_1: dict(
        title="Агентство недвижимости",
        subtitle="Казань · AI-агент на YandexGPT",
        badge="КЕЙС 01",
        stats=[("3 сек", "вместо 40 мин"),
               ("+30%",  "просмотры"),
               ("+22%",  "конверсия")],
    ),
    IMG_CASE_2: dict(
        title="Детейлинг-центр",
        subtitle="Онлайн-запись + финдашборд",
        badge="КЕЙС 02",
        stats=[("+35%", "загрузка"),
               ("0",    "накладок"),
               ("24/7", "запись")],
    ),
    IMG_CASE_3: dict(
        title="Застройщик",
        subtitle="Конвейер заявок + автопрогрев",
        badge="КЕЙС 03",
        stats=[("0",     "потерь"),
               ("+18%",  "сделок"),
               ("5 в 1", "канал")],
    ),
}


# ---------- Вспомогательные функции отрисовки ----------
def _hex(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _load_font(size: int, bold: bool = False):
    """Подбирает системный шрифт."""
    names = (["segoeuib.ttf", "arialbd.ttf", "calibrib.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "calibri.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_w(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _draw_center(draw, text: str, font, fill, cx: int, y: int) -> None:
    w = _text_w(draw, text, font)
    draw.text((cx - w // 2, y), text, font=font, fill=fill)


def _draw_tracked_center(draw, text: str, font, fill, cx: int, y: int,
                         tracking: int = 10) -> None:
    """Текст по центру с умеренной разрядкой (для коротких заголовков)."""
    widths = [_text_w(draw, ch, font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = cx - total // 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def _fit_font(draw, text: str, max_width: int, start_size: int,
              bold: bool = True, min_size: int = 24):
    """Подбирает размер шрифта, чтобы текст влез в max_width."""
    size = start_size
    while size > min_size:
        font = _load_font(size, bold)
        if _text_w(draw, text, font) <= max_width:
            return font
        size -= 4
    return _load_font(min_size, bold)


def _glow(img, cx: int, cy: int, radius: int,
          color: tuple[int, int, int], alpha: int = 120) -> None:
    """Мягкое радиальное свечение на фон."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
               fill=(*color, alpha))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius // 3))
    img.alpha_composite(overlay)


def _blend_rect(img, rect, radius: int, fill_rgba, outline_rgba=None) -> None:
    """Полупрозрачный скруглённый прямоугольник через alpha_composite."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    if outline_rgba:
        od.rounded_rectangle(rect, radius=radius,
                             fill=fill_rgba, outline=outline_rgba, width=2)
    else:
        od.rounded_rectangle(rect, radius=radius, fill=fill_rgba)
    img.alpha_composite(overlay)


def _gradient_bg(W: int, H: int, top: tuple, bottom: tuple) -> Image.Image:
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / max(H - 1, 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        d.line([(0, y), (W, y)], fill=(r, g, b))
    return img


def _draw_logo_mark(draw, x: int, y: int, size: int, accent) -> None:
    draw.rounded_rectangle([x, y, x + size, y + size],
                           radius=size // 4, fill=accent)
    font = _load_font(int(size * 0.55), bold=True)
    bbox = draw.textbbox((0, 0), "S", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x + (size - tw) // 2, y + (size - th) // 2 - bbox[1]),
              "S", font=font, fill=(255, 255, 255))


def _draw_chip(img, draw, x: int, y: int, text: str, font,
               text_color=(255, 255, 255),
               bg_rgba=(255, 255, 255, 36),
               border_rgba=(255, 255, 255, 90)) -> int:
    """Одна «пилюля» с текстом. Возвращает её ширину."""
    pad_x, pad_y = 30, 18
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    w = tw + pad_x * 2
    h = th + pad_y * 2
    rect = [x, y, x + w, y + h]
    _blend_rect(img, rect, h // 2, bg_rgba, border_rgba)
    draw.text((x + pad_x, y + pad_y - bbox[1]),
              text, font=font, fill=text_color)
    return w


def _draw_chips_row(img, draw, chips: list[str], cx: int, y: int,
                    font_size: int = 34, gap: int = 18) -> None:
    """Ряд чипов по центру. Автоуменьшение шрифта, если не помещается."""
    max_w = img.width - 80
    size = font_size
    while size > 20:
        font = _load_font(size)
        widths = [_text_w(draw, c, font) + 60 for c in chips]
        total = sum(widths) + gap * (len(chips) - 1)
        if total <= max_w:
            break
        size -= 2
    font = _load_font(size)
    real_widths = []
    for c in chips:
        bbox = draw.textbbox((0, 0), c, font=font)
        real_widths.append(bbox[2] - bbox[0] + 60)
    total = sum(real_widths) + gap * (len(chips) - 1)
    x = cx - total // 2
    for c, w in zip(chips, real_widths):
        _draw_chip(img, draw, x, y, c, font)
        x += w + gap


def _make_placeholder(path: str, title: str, subtitle: str = "", *,
                      badge: str | None = None,
                      stats: list[tuple[str, str]] | None = None,
                      chips: list[str] | None = None,
                      size: tuple[int, int] = (1280, 720)) -> None:
    """Создаёт крупную читаемую заглушку и сохраняет в JPG."""
    W, H = size
    accent1   = _hex("#6366F1")
    accent2   = _hex("#22D3EE")
    white     = (255, 255, 255)
    secondary = _hex("#A8B4C8")

    # Фон + свечения
    img = _gradient_bg(W, H, _hex("#0B0F1A"), _hex("#1A1F3D")).convert("RGBA")
    _glow(img, int(W * 0.88), int(H * 0.10), 360, accent1, 130)
    _glow(img, int(W * 0.10), int(H * 0.95), 300, accent2, 70)
    draw = ImageDraw.Draw(img)

    # Верхняя и нижняя акцентные полоски
    draw.rectangle([0, 0, W, 8], fill=accent1)
    draw.rectangle([0, H - 8, W, H], fill=accent2)

    # Логотип в левом верхнем углу (крупнее)
    _draw_logo_mark(draw, 40, 40, 80, accent1)
    f_brand = _load_font(38, bold=True)
    f_tag   = _load_font(18)
    draw.text((40 + 80 + 20, 52), "STRYX", font=f_brand, fill=white)
    draw.text((40 + 80 + 20, 102), "AUTOMATION", font=f_tag, fill=secondary)

    # Бейдж в правом верхнем углу
    if badge:
        f_b  = _load_font(30, bold=True)
        bw   = _text_w(draw, badge, f_b) + 48
        bh   = 58
        bx   = W - 40 - bw
        _blend_rect(img, [bx, 40, bx + bw, 40 + bh], 29,
                    (99, 102, 241, 100), (99, 102, 241, 180))
        draw.text((bx + 24, 40 + 12), badge, font=f_b, fill=white)

    # Заголовок — очень крупный, авто-fit
    title_txt = title if badge else title.upper()
    f_title = _fit_font(draw, title_txt, W - 160, 140, bold=True, min_size=60)
    y_title = H // 2 - (160 if stats else 100)
    if badge:
        _draw_center(draw, title_txt, f_title, white, W // 2, y_title)
    else:
        _draw_tracked_center(draw, title_txt, f_title, white,
                             W // 2, y_title, tracking=12)

    # Подзаголовок — крупный
    if subtitle:
        f_sub = _fit_font(draw, subtitle, W - 160, 50, bold=False, min_size=28)
        _draw_center(draw, subtitle, f_sub, secondary,
                     W // 2, y_title + 170)

    # Статистика (для кейсов) — каждое значение подгоняется под колонку
    if stats:
        f_lbl = _load_font(30)
        y_stat = H // 2 + 100
        step   = W // (len(stats) + 1)
        col_w  = step - 40
        for i, (v, l) in enumerate(stats):
            cx = step * (i + 1)
            f_val = _fit_font(draw, v, col_w, 130, bold=True, min_size=44)
            _draw_center(draw, v, f_val, accent2, cx, y_stat)
            _draw_center(draw, l, f_lbl, secondary, cx, y_stat + 150)

    # Чипы (для welcome/about)
    if chips:
        _draw_chips_row(img, draw, chips, W // 2, H - 140, font_size=34)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=92)


def ensure_images() -> None:
    """Генерирует заглушки для отсутствующих изображений."""
    if not PILLOW_OK:
        log.warning("Pillow не установлен — автогенерация заглушек пропущена.")
        return
    os.makedirs("images", exist_ok=True)
    for path, meta in IMAGE_META.items():
        if os.path.exists(path):
            continue
        try:
            _make_placeholder(path, **meta)
            log.info(f"Сгенерирована заглушка: {path}")
        except Exception as e:
            log.warning(f"Не удалось создать заглушку {path}: {e}")


# ============================================================
# SQLite
# ============================================================
def init_db() -> None:
    conn = sqlite3.connect("leads.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id        INTEGER,
            username     TEXT,
            niche        TEXT,
            channels     TEXT,
            speed        TEXT,
            contact      TEXT,
            created_at   TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_lead(tg_id: int, username: str, niche: str,
              channels: str, speed: str, contact: str) -> int:
    conn = sqlite3.connect("leads.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO leads (tg_id, username, niche, channels, speed, contact, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (tg_id, username, niche, channels, speed, contact,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()
    return lead_id


# ============================================================
# FSM
# ============================================================
class AuditFSM(StatesGroup):
    niche    = State()
    channels = State()
    speed    = State()
    contact  = State()


# ============================================================
# Утилита: безопасная отправка фото
# ============================================================
async def safe_send_photo(chat_id: int, path: str, caption: str,
                          reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if os.path.exists(path):
        await bot.send_photo(
            chat_id=chat_id,
            photo=FSInputFile(path),
            caption=caption,
            reply_markup=reply_markup,
        )
    else:
        log.warning(f"Файл {path} не найден, отправляю текст.")
        await bot.send_message(chat_id, caption, reply_markup=reply_markup)


# ============================================================
# Клавиатуры
# ============================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👤 Обо мне",              callback_data="menu:about")
    kb.button(text="🛠 Что я делаю",          callback_data="menu:services")
    kb.button(text="📈 Кейсы",                callback_data="menu:cases")
    kb.button(text="💼 Тарифы",               callback_data="menu:pricing")
    kb.button(text="🎁 Бесплатный аудит",     callback_data="menu:audit")
    kb.button(text="✉️ Написать напрямую",    url=PERSONAL_LINK)
    kb.adjust(1, 1, 1, 1, 1, 1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data="menu:back")
    return kb.as_markup()


def services_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Telegram-боты для заявок",       callback_data="srv:1")
    kb.button(text="🧠 AI-агенты (YandexGPT/GigaChat)", callback_data="srv:2")
    kb.button(text="🔀 Конвейеры заявок → CRM",         callback_data="srv:3")
    kb.button(text="🔥 Автопрогрев «спящих»",           callback_data="srv:4")
    kb.button(text="📊 Финансовые дашборды",            callback_data="srv:5")
    kb.button(text="⬅️ Назад", callback_data="menu:back")
    kb.adjust(1, 1, 1, 1, 1, 1)
    return kb.as_markup()


def cases_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏢 Агентство недвижимости", callback_data="case:1")
    kb.button(text="🚗 Детейлинг-центр",        callback_data="case:2")
    kb.button(text="🏗 Застройщик",             callback_data="case:3")
    kb.button(text="⬅️ Назад", callback_data="menu:back")
    kb.adjust(1, 1, 1, 1)
    return kb.as_markup()


def pricing_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Обсудить тариф", url=PERSONAL_LINK)
    kb.button(text="⬅️ Назад", callback_data="menu:back")
    kb.adjust(1, 1)
    return kb.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить", callback_data="audit:cancel")
    return kb.as_markup()


# ============================================================
# /start
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    caption = (
        "👋 <b>Привет!</b>\n"
        "Это официальный бот <b>Stryx Automation</b>.\n\n"
        "⚙️ Автоматизируем продажи так, чтобы <i>ни одна заявка не терялась</i>.\n\n"
        "🎯 <b>Для кого:</b>\n"
        "• Агентства недвижимости\n"
        "• Детейлинг и автосервисы\n"
        "• Застройщики\n"
        "• Онлайн-школы и услуги\n\n"
        "👇 Выберите, что вам интересно:"
    )
    await safe_send_photo(message.chat.id, IMG_WELCOME, caption, main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🏠 <b>Главное меню</b>", reply_markup=main_menu_kb())


# ============================================================
# Возврат в главное меню
# ============================================================
@router.callback_query(F.data == "menu:back")
async def back_to_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    caption = (
        "🏠 <b>Главное меню</b>\n\n"
        "Выберите интересующий раздел 👇"
    )
    await safe_send_photo(call.message.chat.id, IMG_WELCOME, caption, main_menu_kb())
    await call.answer()


# ============================================================
# Раздел «Обо мне»
# ============================================================
@router.callback_query(F.data == "menu:about")
async def about(call: CallbackQuery) -> None:
    text = (
        "👤 <b>Обо мне</b>\n\n"
        "<b>Stryx Automation</b> — студия автоматизации бизнес-процессов. "
        "Превращаем хаос из чатов и таблиц в чистые, работающие системы.\n\n"
        "🎯 <b>Миссия:</b> чтобы <i>ни одна заявка не терялась</i> "
        "и каждый клиент получал ответ мгновенно — <b>24/7</b>.\n\n"
        "🧰 <b>Стек технологий:</b>\n"
        "• <b>aiogram 3</b> — Telegram-боты\n"
        "• <b>n8n</b> — конвейеры и интеграции\n"
        "• <b>YandexGPT / GigaChat</b> — AI-агенты\n"
        "• <b>amoCRM / Bitrix24</b> — CRM и дашборды\n"
        "• <b>SQL / Google Sheets</b> — аналитика\n\n"
        "📈 <b>Опыт:</b> 30+ реализованных проектов в недвижимости, "
        "авто-бизнесе и услугах. Работаем под ключ: от ТЗ до запуска и поддержки."
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await safe_send_photo(call.message.chat.id, IMG_ABOUT, text, back_kb())
    await call.answer()


# ============================================================
# Раздел «Что я делаю»
# ============================================================
@router.callback_query(F.data == "menu:services")
async def services(call: CallbackQuery) -> None:
    text = (
        "🛠 <b>Что я делаю</b>\n\n"
        "Выберите услугу, чтобы узнать подробнее 👇"
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, text, reply_markup=services_kb())
    await call.answer()


SERVICES = {
    "1": (
        "🤖 <b>Telegram-боты для обработки заявок</b>\n\n"
        "Разрабатываю боты, которые принимают заявки 24/7, "
        "квалифицируют клиента, отправляют уведомления менеджерам "
        "и автоматически создают сделки в CRM.\n\n"
        "✅ <b>Что получите:</b>\n"
        "• Приём заявок без задержек\n"
        "• Автоквалификация клиента\n"
        "• Мгновенные уведомления\n"
        "• Полная статистика по заявкам"
    ),
    "2": (
        "🧠 <b>AI-агенты (YandexGPT / GigaChat)</b>\n\n"
        "Умные ассистенты, которые ведут первичный диалог с клиентом, "
        "отвечают на вопросы, бронируют время и передают горячих лидов менеджеру.\n\n"
        "✅ <b>Что получите:</b>\n"
        "• Ответы клиентам 24/7\n"
        "• Снижение нагрузки на менеджеров\n"
        "• Рост конверсии в диалоге"
    ),
    "3": (
        "🔀 <b>Конвейеры заявок → CRM</b>\n\n"
        "Объединяю все источники заявок в единую систему: "
        "<b>сайт, WhatsApp, Instagram, Авито</b> → <b>CRM</b>.\n\n"
        "✅ <b>Что получите:</b>\n"
        "• Ни одна заявка не теряется\n"
        "• Единое окно для менеджеров\n"
        "• Автоматическая постановка задач"
    ),
    "4": (
        "🔥 <b>Автопрогрев и возврат «спящих» клиентов</b>\n\n"
        "Настраиваю сценарии касаний: поздравления, акции, "
        "персональные напоминания. Возвращаю тех, кто «остыл».\n\n"
        "✅ <b>Что получите:</b>\n"
        "• Реактивация базы\n"
        "• Дополнительные сделки из старой базы\n"
        "• Рост LTV клиента"
    ),
    "5": (
        "📊 <b>Финансовые дашборды</b>\n\n"
        "Собираю дашборды по выручке, каналам и конверсии. "
        "Все ключевые метрики — в одном окне, всегда под рукой.\n\n"
        "✅ <b>Что получите:</b>\n"
        "• Прозрачные показатели\n"
        "• Решения на основе данных\n"
        "• Контроль каждого канала"
    ),
}


@router.callback_query(F.data.startswith("srv:"))
async def service_detail(call: CallbackQuery) -> None:
    key = call.data.split(":")[1]
    text = SERVICES.get(key, "Информация недоступна.")
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Обсудить услугу", url=PERSONAL_LINK)
    kb.button(text="⬅️ К списку услуг", callback_data="menu:services")
    kb.adjust(1, 1)
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, text, reply_markup=kb.as_markup())
    await call.answer()


# ============================================================
# Раздел «Кейсы»
# ============================================================
@router.callback_query(F.data == "menu:cases")
async def cases(call: CallbackQuery) -> None:
    text = (
        "📈 <b>Кейсы</b>\n\n"
        "Выберите кейс, чтобы увидеть детали: проблема → решение → результат 👇"
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, text, reply_markup=cases_kb())
    await call.answer()


CASES = {
    "1": {
        "img": IMG_CASE_1,
        "text": (
            "🏢 <b>Кейс №1. Агентство недвижимости (Казань)</b>\n\n"
            "❗️<b>Проблема:</b>\n"
            "Менеджеры отвечали на заявки в среднем <b>40 минут</b>. "
            "Клиенты уходили к конкурентам, конверсия падала.\n\n"
            "🛠 <b>Решение:</b>\n"
            "Внедрили Telegram-бота с AI-агентом на YandexGPT. "
            "Бот отвечает мгновенно, квалифицирует клиента и передаёт "
            "горячих лидов менеджеру.\n\n"
            "📊 <b>Результат:</b>\n"
            "• Время ответа: <b>40 минут → 3 секунды</b>\n"
            "• Просмотры объектов: <b>+30%</b>\n"
            "• Рост конверсии в показ: <b>+22%</b>"
        ),
    },
    "2": {
        "img": IMG_CASE_2,
        "text": (
            "🚗 <b>Кейс №2. Детейлинг-центр</b>\n\n"
            "❗️<b>Проблема:</b>\n"
            "Записи велись вручную в WhatsApp. Постоянные накладки, "
            "двойные брони, потерянные клиенты.\n\n"
            "🛠 <b>Решение:</b>\n"
            "Разработали Telegram-бота для онлайн-записи с интеграцией "
            "в календарь + финансовый дашборд по услугам и мастерам.\n\n"
            "📊 <b>Результат:</b>\n"
            "• Загрузка мастеров: <b>+35%</b>\n"
            "• Накладки броней: <b>0</b>\n"
            "• Выручка видна в реальном времени"
        ),
    },
    "3": {
        "img": IMG_CASE_3,
        "text": (
            "🏗 <b>Кейс №3. Застройщик</b>\n\n"
            "❗️<b>Проблема:</b>\n"
            "Заявки приходили из 5 источников (сайт, Авито, Instagram, "
            "звонки, рекомендации). Половина терялась, лиды «остывали».\n\n"
            "🛠 <b>Решение:</b>\n"
            "Собрали единый конвейер заявок в CRM + запустили автопрогрев "
            "«спящих» клиентов через Telegram-бота.\n\n"
            "📊 <b>Результат:</b>\n"
            "• Потерянных заявок: <b>0</b>\n"
            "• Возврат «спящих»: <b>+18% сделок</b>\n"
            "• Прозрачная аналитика по всем каналам"
        ),
    },
}


@router.callback_query(F.data.startswith("case:"))
async def case_detail(call: CallbackQuery) -> None:
    key = call.data.split(":")[1]
    data = CASES.get(key)
    if not data:
        await call.answer("Кейс не найден", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Хочу так же", url=PERSONAL_LINK)
    kb.button(text="⬅️ К списку кейсов", callback_data="menu:cases")
    kb.adjust(1, 1)
    try:
        await call.message.delete()
    except Exception:
        pass
    await safe_send_photo(call.message.chat.id, data["img"], data["text"], kb.as_markup())
    await call.answer()


# ============================================================
# Раздел «Тарифы»
# ============================================================
@router.callback_query(F.data == "menu:pricing")
async def pricing(call: CallbackQuery) -> None:
    text = (
        "💼 <b>Тарифы Stryx Automation</b>\n\n"
        "Выберите подходящий формат сотрудничества.\n"
        "Все тарифы — <i>под ключ</i>, с поддержкой после запуска.\n\n"

        "🚀 <b>«Старт» — 25 000 ₽</b>\n"
        "<i>Для первого шага в автоматизации</i>\n"
        "• Telegram-бот для приёма заявок\n"
        "• Уведомления менеджеру\n"
        "• Базовая аналитика\n"
        "• Срок: 5–7 дней\n\n"

        "💎 <b>«Бизнес» — 45 000 ₽</b>\n"
        "<i>Оптимально для растущего бизнеса</i>\n"
        "• Всё из «Старт»\n"
        "• AI-агент на YandexGPT/GigaChat\n"
        "• Интеграция с CRM (amoCRM/Bitrix24)\n"
        "• Конвейер из 2–3 источников заявок\n"
        "• Срок: 10–14 дней\n\n"

        "👑 <b>«Премиум» — 70 000 ₽</b>\n"
        "<i>Максимальная автоматизация</i>\n"
        "• Всё из «Бизнес»\n"
        "• Конвейер из всех источников (сайт, WhatsApp, Instagram, Авито)\n"
        "• Автопрогрев и возврат «спящих» клиентов\n"
        "• Финансовый дашборд\n"
        "• 1 месяц поддержки включён\n"
        "• Срок: 3–4 недели\n\n"
        "👇 Нажмите кнопку ниже, чтобы обсудить детали."
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, text, reply_markup=pricing_kb())
    await call.answer()


# ============================================================
# Бесплатный аудит (FSM)
# ============================================================
@router.callback_query(F.data == "menu:audit")
async def audit_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuditFSM.niche)
    text = (
        "🎁 <b>Бесплатный аудит вашего бизнеса</b>\n\n"
        "Задам <b>4 коротких вопроса</b> — на выходе вы получите "
        "персональный разбор с точками роста автоматизации.\n\n"
        "<i>Займёт меньше минуты.</i>\n\n"
        "1️⃣ <b>Какая у вас ниша / сфера бизнеса?</b>\n"
        "Например: недвижимость, детейлинг, услуги, e-com..."
    )
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(call.message.chat.id, text, reply_markup=cancel_kb())
    await call.answer()


@router.message(AuditFSM.niche)
async def audit_niche(message: Message, state: FSMContext) -> None:
    await state.update_data(niche=message.text.strip())
    await state.set_state(AuditFSM.channels)
    await message.answer(
        "2️⃣ <b>Откуда к вам приходят заявки?</b>\n"
        "Перечислите каналы: сайт, WhatsApp, Instagram, Авито, звонки...",
        reply_markup=cancel_kb(),
    )


@router.message(AuditFSM.channels)
async def audit_channels(message: Message, state: FSMContext) -> None:
    await state.update_data(channels=message.text.strip())
    await state.set_state(AuditFSM.speed)
    await message.answer(
        "3️⃣ <b>Как быстро вы отвечаете на заявку в среднем?</b>\n"
        "Например: «сразу», «10 минут», «2 часа», «иногда забываем».",
        reply_markup=cancel_kb(),
    )


@router.message(AuditFSM.speed)
async def audit_speed(message: Message, state: FSMContext) -> None:
    await state.update_data(speed=message.text.strip())
    await state.set_state(AuditFSM.contact)
    await message.answer(
        "4️⃣ <b>Куда прислать разбор?</b>\n"
        "Оставьте Telegram-юзернейм или телефон для связи.",
        reply_markup=cancel_kb(),
    )


@router.message(AuditFSM.contact)
async def audit_contact(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()

    user = message.from_user
    lead_id = save_lead(
        tg_id=user.id,
        username=f"@{user.username}" if user.username else "—",
        niche=data.get("niche", "—"),
        channels=data.get("channels", "—"),
        speed=data.get("speed", "—"),
        contact=message.text.strip(),
    )

    await message.answer(
        "✅ <b>Спасибо! Заявка принята.</b>\n\n"
        "Мы изучим ваш запрос и вернёмся с персональным разбором "
        "в ближайшее время.\n\n"
        "А пока — можете написать напрямую 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✉️ Написать напрямую", url=PERSONAL_LINK)],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="menu:back")],
        ]),
    )

    admin_text = (
        "🔔 <b>НОВАЯ ЗАЯВКА НА АУДИТ</b>\n\n"
        f"🆔 <b>Lead ID:</b> {lead_id}\n"
        f"👤 <b>Пользователь:</b> {user.full_name}\n"
        f"🔗 <b>Username:</b> @{user.username if user.username else '—'}\n"
        f"🆔 <b>TG ID:</b> <code>{user.id}</code>\n\n"
        f"🏢 <b>Ниша:</b> {data.get('niche')}\n"
        f"📥 <b>Каналы заявок:</b> {data.get('channels')}\n"
        f"⚡️ <b>Скорость ответа:</b> {data.get('speed')}\n"
        f"📞 <b>Контакт:</b> {message.text.strip()}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text)
    except Exception as e:
        log.error(f"Не удалось отправить уведомление админу: {e}")


@router.callback_query(F.data == "audit:cancel")
async def audit_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await bot.send_message(
        call.message.chat.id,
        "❌ Аудит отменён. Если захотите — вернёмся в любой момент.",
        reply_markup=main_menu_kb(),
    )
    await call.answer()


# ============================================================
# Заглушка для прочих сообщений
# ============================================================
@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return
    await message.answer(
        "🤖 Чтобы начать — нажмите /start или выберите пункт меню.",
        reply_markup=main_menu_kb(),
    )


# ============================================================
# WEBHOOK для Render
# ============================================================
async def healthcheck(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def on_startup(bot: Bot) -> None:
    if not BASE_URL:
        log.warning("BASE_URL не задан. Webhook не будет установлен.")
        return
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    log.info(f"Webhook установлен: {webhook_url}")


def main() -> None:
    init_db()
    ensure_images()

    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/health", healthcheck)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path="/webhook")

    setup_application(app, dp, bot=bot)

    port = int(os.getenv("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
