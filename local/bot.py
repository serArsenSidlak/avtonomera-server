"""Local MVP Telegram bot — single-screen UI with a step-by-step search wizard.

UX (per user requests):
* ONE persistent "screen" message per chat; navigation EDITS it in place.
* Every user text input is DELETED after processing → the chat stays clean.
* Search is a guided wizard: Тип → Регіон → Ціна → ТСЦ → бажана комбінація → результати.
  Price is its own step, available for any vehicle type. Digit masks (-/*) supported.
* TSC address is hidden in results until the «📍 Адреса» button is pressed.

Run:  LOCAL_BOT_TOKEN=... python -m local.bot   (token SEPARATE from the dev-comms bot)
"""
from __future__ import annotations

import asyncio
from typing import Dict, Optional, Tuple
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from local import config, db
from local.plate import parse_plate, pattern_to_match

# Ensure an event loop exists before aiogram's Dispatcher is built at import time.
# (uvloop, pulled in by uvicorn[standard], makes get_event_loop() raise without one.)
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

dp = Dispatcher(storage=MemoryStorage())

# chat_id -> screen message_id (the single message we keep editing).
_screens: Dict[int, int] = {}
# Filled at startup from get_me(), used to build referral deep-links.
BOT_USERNAME = "nomer_na_avto_bot"

STEP_ORDER = ["type", "region", "series", "endseries", "price", "combo"]

# Official region letter-pairs (Додаток 4 до Вимог до НЗ, МВС) — Cyrillic. These are ALL the
# series a region can have, so a user can monitor a series even before any plate is available.
#   Коди регіонів — ПЕРШІ 2 літери (Додаток 4 до Вимог, дослівно з офіційного наказу МВС, з PDF Артура).
#   Це адміністративно-територіальна належність (регіон), а НЕ «серія» (серія = кінцеві літери, Додаток 5).
# Коди регіонів (перші 2 літери). Перевірено по 20+ млн реальних номерів у БД:
# кожен регіон має пару кодів (А↔К / В↔Н / С↔І з тим самим індикатором-2-ю літерою).
REGION_SERIES: dict = {
    "АР Крим": ["АК", "КК"],
    "Вінницька": ["АВ", "КВ"],
    "Волинська": ["АС", "КС"],
    "Дніпропетровська": ["АЕ", "КЕ"],
    "Донецька": ["АН", "КН"],
    "Житомирська": ["АМ", "КМ"],
    "Закарпатська": ["АО", "КО"],
    "Запорізька": ["АР", "КР"],
    "Івано-Франківська": ["АТ", "КТ"],
    "Київська": ["АІ", "КІ"],
    "м. Київ": ["АА", "КА"],
    "Кіровоградська": ["ВА", "НА"],
    "Луганська": ["ВВ", "НВ"],
    "Львівська": ["ВС", "НС"],
    "Миколаївська": ["ВЕ", "НЕ"],
    "Одеська": ["ВН", "НН"],
    "Полтавська": ["ВІ", "НІ"],
    "Рівненська": ["ВК", "НК"],
    "Сумська": ["ВМ", "НМ"],
    "Тернопільська": ["ВО", "НО"],
    "Харківська": ["АХ", "КХ"],
    "Херсонська": ["ВТ", "НТ"],
    "Хмельницька": ["ВХ", "НХ"],
    "Черкаська": ["СА", "ІА"],
    "Чернігівська": ["СВ", "ІВ"],
    "Чернівецька": ["СЕ", "ІЕ"],
    "м. Севастополь": ["СН", "ІН"],
}

# Додаток 5 — тип ТЗ кодується в КІНЦЕВІЙ серії (останні 2 літери); перша літера суфікса:
#   Y або Z → Електромобіль; R (або ZА) → Електромотоцикл; J або L → Мотоцикл;
#   Х+латинська (ХJ,ХL,ХF…) або F → Причіп; решта (кирилична) → Легковий, вантажний.
TYPE_SERIES_PREFIX: dict = {
    "Електромобіль": ["Y", "Z"],
    "Електромотоцикл": ["R"],
    "Мотоцикл": ["J", "L"],
    "Причіп": ["Х+лат", "F"],
    "Легковий, вантажний": ["кирилична"],
}

# Додаток 5 (постанова МВС) — ТОЧНІ серії (кінцеві 2 літери) для кожного типу ТЗ.
_D5_BLOCKS: dict = {
    "Легковий, вантажний": (
        "ААВАСАЕАНАІА КАМАРАТАХАОО АВВВСВЕВНВ ІВ КВМВРВ ТВХВОР АСВСССЕСНС ІС КСМСРС ТСХСОТ "
        "АЕ ВЕ СЕ ЕЕ НЕ ІЕ КЕМЕРЕ ТЕ ХЕОХ АНВНСНЕНННІН КНМНРНТНХН АІ ВІ СІ ЕІ НІ ІІ КІ МІ РІ ТІ ХІ "
        "АКВКСКЕКНК ІК ККМКРК ТКХК АМВМСМЕМНМІМКМММРМТМХМ АОВОСОЕОНОІО КОМОРОТОХО "
        "АР ВР СР ЕР НР ІР КР МР РР ТР ХР АТВТ СТ ЕТ НТ ІТ КТМТРТ ТТ ХТ АХВХСХЕХНХІХ КХМХРХТХХХ "
        "ОА ОВ ОС ОЕ ОН ОІ ОК ОМ"),
    "Причіп": "XFXGXJXLXNXRXSXUXVXYXZ FF FR FSFUFVFYFZ СFСGСJ СLСNСRСSСUСY FG FJ FL FN",
    "Електромобіль": (
        "UAUFUGUHUIUJUKULUMUNUOUP URUSUTUUUХUY QAQBQCQDQEQFQGQHQIQJQKQL QMQNQOQPQQQRQSQTQUQХQY "
        "ZAZBZCZDZEZFZGZHZI ZJZKZL ZMZNZOZPZRZSZTZUZVZXZYZZ YAYBYCYDYEYFYGYHYIYJYKYL "
        "YMYNYOYPYRYSYTYUYVYXYYYZ UB UC UD UE"),
    "Мотоцикл": ("JAJBJCJDJE JFJGJH JI JJ JKJL JMJNJOJPJRJS JTJUJVJXJYJZ "
                 "LELFLGLHLI LJLKLLLMLNLOLP LRLSLTLULVLXLYLZ"),
    "Електромотоцикл": ("RARFRGRHRIRJRKRLRMRNRORP RRRSRTRURVRXRYRZ "
                        "SASBSCSDSESFSGSHSI SJSKSL SMSNSOSPSRSSSTSUSVSXSYSZ"),
}
_SERIES_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                                 "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})


def _official_series_for(vtype: str) -> list:
    """Офіційні серії (кінцеві літери) для типу ТЗ за Додатком 5."""
    letters = _D5_BLOCKS.get(vtype, "").replace(" ", "")
    return sorted({letters[i:i + 2].translate(_SERIES_LAT2CYR) for i in range(0, len(letters) - 1, 2)})


TYPE_SERIES_OFFICIAL: dict = {vt: _official_series_for(vt) for vt in _D5_BLOCKS}


def _region_for_plate(plate: Optional[str]) -> str:
    """Назва регіону за першими 2 літерами номера (код регіону, Додаток 4)."""
    if not plate or len(plate) < 2:
        return ""
    pref = plate[:2].upper()
    for region, pairs in REGION_SERIES.items():
        if pref in pairs:
            return region
    return ""


def _region_series(region: Optional[str]) -> list:
    """Official series pairs for a region name (tolerant of 'Київ'/'м. Київ' variants)."""
    if not region:
        return []
    if region in REGION_SERIES:
        return REGION_SERIES[region]
    r = region.replace("м.", "").replace("область", "").strip()
    for k, v in REGION_SERIES.items():
        kk = k.replace("м.", "").replace("область", "").strip()
        if kk == r or kk.startswith(r) or r.startswith(kk):
            return v
    return []


class Flow(StatesGroup):
    """Conversation states for steps that need free-text input."""

    search = State()
    new_hunt = State()
    report = State()
    acheck = State()
    word = State()
    combo = State()
    combo_series = State()
    endseries = State()
    admin_addadmin = State()
    admin_broadcast = State()
    admin_vip_user = State()
    admin_vip_days = State()
    admin_csv = State()


@dp.callback_query.middleware
async def _adopt_screen(handler, event, data):
    """Treat the message a button was pressed on as THE screen, so we always edit it.

    This keeps the UI to a single message even after a bot restart (when the in-memory
    screen map is empty) — we adopt whatever message the user is interacting with.
    """
    if getattr(event, "message", None) is not None:
        _screens[event.message.chat.id] = event.message.message_id
    return await handler(event, data)


# ── keyboards / screen management ─────────────────
def kb_main() -> InlineKeyboardMarkup:
    """Main menu — 4 grouped categories (details inside each section)."""
    b = InlineKeyboardBuilder()
    b.button(text="🔍 Пошук", callback_data="m_search")
    b.button(text="🔔 Моніторинг", callback_data="m_monitor")
    b.button(text="🚗 Перевірка авто", callback_data="acheck")
    b.button(text="⚙️ Ще", callback_data="m_more")
    b.adjust(2, 1, 1)
    return b.as_markup()


def _kb_submenu(items: list) -> InlineKeyboardMarkup:
    """Build a section submenu keyboard (2 per row) + a back-to-menu button."""
    b = InlineKeyboardBuilder()
    for text, data in items:
        b.button(text=text, callback_data=data)
    b.button(text="⬅️ Меню", callback_data="menu")
    n = len(items)
    rows = [2] * (n // 2) + ([1] if n % 2 else []) + [1]
    b.adjust(*rows)
    return b.as_markup()


def kb_back(extra: Optional[list] = None) -> InlineKeyboardMarkup:
    """Keyboard with optional extra buttons plus a 'menu' button."""
    b = InlineKeyboardBuilder()
    for text, data in (extra or []):
        b.button(text=text, callback_data=data)
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    return b.as_markup()


async def show(bot: Bot, chat_id: int, text: str, kb: InlineKeyboardMarkup) -> None:
    """Edit the chat's single screen message, or create it if missing/lost.

    The screen message id is persisted in the DB so it survives бот-рестарти/деплої —
    інакше після кожного перезапуску бот «забував» екран і слав НОВЕ повідомлення
    (звідси «чистий екран не завжди працює»). На збій редагування — прибираємо старий
    екран, щоб не лишати сміття.
    """
    mid = _screens.get(chat_id)
    if mid is None:  # памʼять порожня (напр. після рестарту) → беремо з БД
        v = await db.get_meta(f"scr_{chat_id}")
        if v and v.isdigit():
            mid = int(v)
            _screens[chat_id] = mid
    if mid is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():  # той самий вміст — лишаємо як є
                return
            await _safe_delete(bot, chat_id, mid)  # застарілий екран → прибрати
        except Exception:
            await _safe_delete(bot, chat_id, mid)
    msg = await bot.send_message(chat_id, text, reply_markup=kb)
    _screens[chat_id] = msg.message_id
    try:
        await db.set_meta(f"scr_{chat_id}", str(msg.message_id))
    except Exception:  # noqa: BLE001
        pass


async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Delete a message, ignoring failures."""
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


# ── filters helpers ───────────────────────────────
async def _filters(state: FSMContext) -> dict:
    """Return the current search filters dict."""
    return (await state.get_data()).get("f", {})


async def _set_filters(state: FSMContext, f: dict) -> None:
    """Persist the search filters dict."""
    await state.update_data(f=f)


def _price_label(f: dict) -> str:
    """Human label for the current price filter."""
    lo, hi = f.get("price_min"), f.get("price_max")
    if lo is None and hi is None:
        return "будь-яка"
    fmt = lambda v: f"{int(v):,}".replace(",", " ")
    if lo is not None and hi is not None:
        return f"{fmt(lo)} грн" if lo == hi else f"{fmt(lo)}–{fmt(hi)} грн"
    if lo is not None:
        return f"від {fmt(lo)} грн"
    return f"до {fmt(hi)} грн"


def _series_label(f: dict) -> str:
    """Breadcrumb label for the letter filter (front and/or back letters)."""
    s, e = f.get("series"), f.get("series_end")
    if s and e:
        return f"{s}****{e}"
    if e:
        return f"****{e}"
    return s or "всі серії"


def _summary(f: dict) -> str:
    """One-line breadcrumb of chosen filters so far."""
    parts = [
        f"🚗 {f.get('vtype') or 'всі'}",
        f"🌍 {f.get('region') or 'всі'}",
        f"🔤 {_series_label(f)}",
        f"💰 {_price_label(f)}",
    ]
    if f.get("query"):
        parts.append(f"⌨️ {f['query']}")
    return " · ".join(parts)


def _hunt_desc(h: dict) -> str:
    """Short human description of a hunt, e.g. '0*00 (для Електромобіль · по всій Україні)'."""
    digits = h.get("digits_exact") or (h.get("digits_mask") or "").replace("_", "*")
    ls = h.get("letters_start") or ""
    le = h.get("letters_end") or ""
    if le:  # слово на номері: перші + (цифри|****) + кінцеві
        combo = f"{ls}{digits or '****'}{le}"
    else:
        combo = ls + (digits or "")
    if not combo:
        combo = "будь-який"
    parts = ["для " + (h.get("vehicle_type") or "всіх ТЗ"), h.get("region") or "по всій Україні"]
    if h.get("price_min") is not None or h.get("price_max") is not None:
        parts.append(_price_label(h))
    return f"<b>{combo}</b> ({' · '.join(parts)})"


def _fmt_dt(value) -> str:
    """Format an ISO UTC timestamp in Kyiv time as 'DD.MM.YYYY HH:MM' (default TZ: Europe/Kyiv)."""
    s = str(value or "")
    if not s:
        return "—"
    try:
        import datetime as _dt
        d = _dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            d = d.astimezone(ZoneInfo("Europe/Kyiv"))
        except Exception:
            d = d.astimezone(_dt.timezone(_dt.timedelta(hours=3)))  # Kyiv summer fallback
        return d.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return s[:16].replace("T", " ")


def _plate_card(plate: str) -> str:
    """Render a plate as a UA-style card: 🇺🇦 [ АВ 1234 ВН ] in monospace."""
    parts = parse_plate(plate)
    ls = parts.get("letters_start") or ""
    dg = parts.get("digits") or ""
    le = parts.get("letters_end") or ""
    inner = " ".join(p for p in (ls, dg, le) if p) or plate
    return f"🇺🇦 <code>[ {inner} ]</code>"


def _fmt_row(r: dict, show_addr: bool = False) -> str:
    """Format one search-result line; address shown only when ``show_addr``."""
    price = f"{int(r['price']):,} грн".replace(",", " ") if r.get("price") else "—"
    mark = "✅" if r["is_available"] else "❌"
    place = r.get("tsc") or "—"
    if show_addr and r.get("tsc_address"):
        place += f" · {r['tsc_address']}"
    return f"{mark} <b>{r['plate_number']}</b>\n   {r['region']} · {place}\n   💰 {price}"


# ── main menu / static screens ────────────────────
async def render_main(bot: Bot, chat_id: int, banner: str = "") -> None:
    """Render the main menu with a live total. Optional ``banner`` line shown on top."""
    user = await db.get_user(chat_id)
    plan = "💎 PRO" if db.is_pro(user) else "🆓 FREE"
    text = (
        (banner + "\n\n" if banner else "")
        + "🇺🇦 <b>Моніторинг Автономерів</b>\n"
        "━━━━━━━━━━━━━━\n"
        "🔍 Підбір вільних номерів для реєстрації\n"
        "🚗 Перевірка авто за номером або VIN\n"
        "🔔 Моніторинг — сповіщу, щойно зʼявиться\n\n"
        f"💎 {plan}   ·   🟢 24/7\n\n"
        "👇 Обери дію"
    )
    markup = kb_main()
    if await db.is_admin(chat_id):
        markup.inline_keyboard.append(
            [InlineKeyboardButton(text="🛠 Адмінка", callback_data="admin")]
        )
    await show(bot, chat_id, text, markup)


async def push_refresh_all(
    bot: Bot, banner: str = "🔄 <b>Базу оновлено!</b> Спробуй новий пошук 👇"
) -> int:
    """Re-engage every real user: delete their old screen and send a FRESH menu message.

    A *new* message (not an edit) is what bumps the chat back to the top of Telegram's list,
    so the bot stops sinking out of view — while the single-screen tidiness is preserved by
    deleting the previous screen first. Called after a scan ('base updated') and on demand.
    """
    sent = 0
    for chat_id in await db.all_user_ids():
        old = _screens.pop(chat_id, None)  # clear so show() SENDS a new message (bumps chat)
        if old:
            await _safe_delete(bot, chat_id, old)
        try:
            await render_main(bot, chat_id, banner=banner)
            sent += 1
        except Exception as exc:  # user blocked the bot, etc.
            print(f"[refresh] {chat_id}: {exc!r}")
        await asyncio.sleep(0.05)  # stay under Telegram's broadcast rate limit
    return sent


@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    """Greet, register, apply referral deep-link, reset to a fresh single screen."""
    await db.ensure_user(message.chat.id, message.from_user.username if message.from_user else None)
    # Owner always has PRO (test without limits); use a second account to test FREE limits.
    if config.ADMIN_CHAT_ID and message.chat.id == config.ADMIN_CHAT_ID:
        if not db.is_pro(await db.get_user(message.chat.id)):
            await db.grant_pro(message.chat.id, 3650)
    # App link deep-link: /start link_<code> — binds the mobile app to this Telegram account.
    payload = (command.args or "").strip()
    if payload.startswith("link_"):
        code = payload[5:]
        ok = await db.link_bind(code, message.chat.id)
        await message.answer(
            "✅ Додаток привʼязано до твого акаунту! Обране й моніторинги тепер синхронізуються."
            if ok else "🔗 Це посилання вже використане або застаріле."
        )
        # fall through to render the main screen below
    # Referral deep-link: /start ref_<referrer_id>
    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload[4:])
        except ValueError:
            referrer_id = 0
        if referrer_id:
            reward = await db.set_referrer(message.chat.id, referrer_id)
            if reward:
                invited = reward["invited_count"]
                txt = f"🎉 За вашим запрошенням приєднався новий користувач!\nЗапрошено всього: {invited}"
                if invited % db.FRIENDS_PER_HUNT == 0:
                    txt += "\n➕ Вам нараховано +1 слот моніторингу!"
                else:
                    left = db.FRIENDS_PER_HUNT - (invited % db.FRIENDS_PER_HUNT)
                    txt += f"\nЩе {left} друга → +1 моніторинг"
                if reward["pro_days"]:
                    txt += f"\n💎 Вам нараховано PRO на {reward['pro_days']} днів!"
                try:
                    await message.bot.send_message(referrer_id, txt)
                except Exception:
                    pass
    await state.clear()
    _screens.pop(message.chat.id, None)
    # Render the fresh screen FIRST so the chat is never momentarily empty
    # (an empty chat makes Telegram show the description + START overlay).
    await render_main(message.bot, message.chat.id)
    keep = set(await db.notif_message_ids(message.chat.id))
    keep.add(_screens.get(message.chat.id, 0))
    await _wipe_chat(message.bot, message.chat.id, message.message_id, keep=keep)


async def _wipe_chat(bot: Bot, chat_id: int, upto_id: int, keep: Optional[set] = None) -> None:
    """Best-effort delete recent messages (Telegram has no bulk-delete for bots).

    Deletes message ids from ``upto_id`` downwards (~120 back), skipping ``keep`` ids.
    Bots can only delete messages they can access within ~48h, so older history remains.
    """
    keep = keep or set()
    for mid in range(upto_id, max(0, upto_id - 120), -1):
        if mid in keep:
            continue
        await _safe_delete(bot, chat_id, mid)


@dp.message(Command("clear"))
async def cmd_clear(message: Message, state: FSMContext) -> None:
    """Clear recent messages and show a fresh screen (screen rendered first)."""
    await state.clear()
    _screens.pop(message.chat.id, None)
    await render_main(message.bot, message.chat.id)
    keep = {_screens.get(message.chat.id, 0)}
    await _wipe_chat(message.bot, message.chat.id, message.message_id, keep=keep)


@dp.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext) -> None:
    """Ask the user to describe a problem (forwarded to admin)."""
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await state.set_state(Flow.report)
    await show(message.bot, message.chat.id,
              "🐞 <b>Повідомити про помилку</b>\n\nОпиши проблему одним повідомленням — "
              "я перешлю адміну.", kb_back())


@dp.message(Flow.report)
async def do_report(message: Message, state: FSMContext) -> None:
    """Store the report, forward it to the admin, confirm to the user."""
    text = (message.text or "").strip()
    uname = message.from_user.username if message.from_user else None
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await state.clear()
    if text:
        await db.add_report(message.chat.id, uname, text)
        if config.ADMIN_CHAT_ID:
            who = f"@{uname}" if uname else str(message.chat.id)
            try:
                await message.bot.send_message(
                    config.ADMIN_CHAT_ID, f"🐞 <b>Звіт про помилку</b> від {who}:\n\n{text}")
            except Exception:
                pass
    await show(message.bot, message.chat.id,
              "✅ Дякую! Повідомлення надіслано адміну.", kb_back())


# ── AutoCheck (перевірка авто по реєстру МВС, база на ПК через тунель) ──
async def _ac_get(param: str, val: str) -> dict:
    """Single AutoCheck lookup forcing a specific param ('plate' or 'vin')."""
    import aiohttp

    headers = {"X-API-Key": config.API_KEY} if getattr(config, "API_KEY", "") else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8000/autocheck/lookup", params={param: val},
                             headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
                return await r.json()
    except Exception:  # noqa: BLE001
        return {"offline": True}


def _ac_detect(query: str):
    """Decide ('plate'|'vin', normalized value) from a free-text query."""
    import re as _re

    q = (query or "").strip().upper()
    alnum = _re.sub(r"[^A-Z0-9]", "", q)
    # VIN: лат. літери+цифри, ≥10 символів, без кирилиці. Інакше — номер.
    is_vin = bool(_re.search(r"\d", q)) and len(alnum) >= 10 and not _re.search(r"[А-ЯІЇЄҐ]", q)
    return ("vin", alnum) if is_vin else ("plate", _re.sub(r"[\s\-]", "", q))


async def _autocheck_query(query: str) -> dict:
    """Look up by plate or VIN via the server's /autocheck/lookup (PC-agent → server test DB)."""
    param, val = _ac_detect(query)
    return await _ac_get(param, val)


def _full_plate(text: str) -> Optional[str]:
    """Normalized plate if text is a COMPLETE UA plate (XX####XX); else None."""
    import re as _re

    from local.plate import normalize_plate

    p = normalize_plate(text or "")
    # літери: кирилиця + латиниця (електро/спец-серії типу Y,Z,U,Q,R,S,F… лишаються латиницею)
    return p if _re.fullmatch(r"[A-ZА-ЯІЇЄҐ]{2}\d{4}[A-ZА-ЯІЇЄҐ]{2}", p) else None


def _fmt_date(iso: Optional[str]) -> str:
    """ISO YYYY-MM-DD → DD.MM.YYYY for display."""
    if iso and len(iso) >= 10 and iso[4] == "-":
        return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"
    return iso or "—"


_DEREG_KW = ("ЗНЯТ", "ВИБРАКУ", "УТИЛІЗ", "ВИВЕЗЕ", "ПРИПИНЕ", "ВКРАДЕ", "РОЗУКОМПЛЕКТ", "ЗА КОРДОН")


def _reg_status(d: dict) -> tuple:
    """('never'|'active'|'dereg', emoji, label) — статус реєстрації номера/авто."""
    if not d.get("found"):
        return ("never", "⚪", "ніколи не реєструвався")
    h = d.get("history") or []
    last_op = (h[0].get("oper_name") or "").upper() if h else ""
    if any(k in last_op for k in _DEREG_KW):
        return ("dereg", "⚠️", "знятий з обліку (зараз не зареєстрований)")
    return ("active", "✅", "зареєстрований")


def _fmt_ac_summary(d: dict, query: str) -> str:
    """Result screen — завжди з ЯВНИМ статусом номера + ідентифікація + кнопки."""
    if d.get("offline"):
        return ("⏳ База перевірки авто зараз недоступна (агент на ПК вимкнено або не підключений).\n"
                "Спробуй пізніше.")
    wanted = d.get("wanted") or []
    status, semoji, slabel = _reg_status(d)
    head = (query if not d.get("found") else (d.get("vehicle") or {}).get("plate")) or query
    if not d.get("found"):
        lines = [f"{semoji} <b>{head}</b>", "━━━━━━━━━━━━",
                 "Статус: <b>ніколи не реєструвався</b> в реєстрі МВС (з 2013)."]
        if wanted:
            lines.append("\n🚨 <b>АЛЕ авто Є в розшуку!</b> Деталі — кнопка «🚨 Розшук».")
        elif _full_plate(query):
            lines.append("\nЙмовірно <b>вільний</b> — постав на моніторинг, сповіщу про появу 🔔")
        lines.append("\nОбери, що показати 👇")
        return "\n".join(lines)
    v = d.get("vehicle") or {}
    title = f"{v.get('brand') or ''} {v.get('model') or ''}".strip() or "Транспортний засіб"
    lines = []
    if wanted:
        lines.append("🚨 <b>УВАГА: авто в розшуку!</b> Деталі — кнопка «🚨 Розшук».\n")
    lines.append(f"{semoji} Статус: <b>{slabel}</b>")
    yr = f", {v['make_year']}" if v.get("make_year") else ""
    lines.append(f"🚗 <b>{title}</b>{yr}")
    if v.get("plate"):
        lines.append(f"🔢 {v['plate']}")
    mk = d.get("market")
    if mk and (mk.get("median") or mk.get("mean")):
        lines.append(f"💵 ~${(mk.get('median') or mk.get('mean')):,} (AutoRia)".replace(",", " "))
    lines.append("\nОбери, що показати 👇")
    return "\n".join(lines)


def _fmt_ac_reg(d: dict) -> str:
    """Держреєстрація — повні дані поточного авто."""
    if d.get("offline"):
        return "⏳ База перевірки авто зараз недоступна. Спробуй пізніше."
    v = d.get("vehicle") or {}
    if not v:
        return "📋 <b>Держреєстрація</b>\n\nДані відсутні."
    title = f"{v.get('brand') or ''} {v.get('model') or ''}".strip() or "Транспортний засіб"
    _, semoji, slabel = _reg_status(d)
    lines = ["📋 <b>Держреєстрація</b>", f"{semoji} Статус: <b>{slabel}</b>\n", f"🚗 <b>{title}</b>"]
    if v.get("make_year"):
        lines.append(f"📅 Рік випуску: <b>{v['make_year']}</b>")
    spec = []
    if v.get("capacity"):
        spec.append(f"{v['capacity']} см³")
    if v.get("fuel"):
        spec.append(str(v["fuel"]).lower())
    if v.get("color"):
        spec.append(str(v["color"]).lower())
    if spec:
        lines.append("⚙️ " + ", ".join(spec))
    body = " · ".join(x for x in (v.get("kind"), v.get("body")) if x)
    if body:
        lines.append(f"🚙 {body}")
    if v.get("vin"):
        lines.append(f"🔑 VIN: <code>{v['vin']}</code>")
    if v.get("plate"):
        lines.append(f"🔢 Поточний номер: <b>{v['plate']}</b>")
        region = _region_for_plate(v.get("plate"))
        if region:
            lines.append(f"📍 Регіон: {region}")
    if d.get("first_reg"):
        lines.append(f"🗓 Перша реєстрація: {_fmt_date(d['first_reg'])}")
    h = d.get("history") or []
    if h:
        last = h[0]  # сервер віддає найновіші зверху
        op = (last.get("oper_name") or "").capitalize()
        row = f"\n🧾 Остання операція: {_fmt_date(last.get('d_reg'))}"
        if op:
            row += f" — {op}"
        if last.get("dep"):
            row += f" ({last['dep']})"
        lines.append(row)
    mk = d.get("market")
    if mk and (mk.get("median") or mk.get("mean")):
        med = mk.get("median") or mk.get("mean")
        line = f"\n💵 Ринкова ціна (AutoRia): <b>~${med:,}</b>".replace(",", " ")
        if mk.get("p25") and mk.get("p75"):
            line += f"\n   діапазон ${mk['p25']:,}–${mk['p75']:,}".replace(",", " ")
        if mk.get("total"):
            line += f" · {mk['total']} оголошень"
        lines.append(line)
    lines.append("\n<i>Джерела: реєстр МВС (data.gov.ua) + ціни AutoRia.</i>")
    return "\n".join(lines)


def _fmt_ac_roz(d: dict) -> str:
    """Розшук — статус по базі розшуку МВС."""
    if d.get("offline"):
        return "⏳ База перевірки авто зараз недоступна. Спробуй пізніше."
    wanted = d.get("wanted") or []
    if not wanted:
        return ("🚨 <b>Розшук</b>\n\n✅ Авто <b>не значиться</b> в базі розшуку МВС.\n\n"
                "<i>Джерело: відкритий датасет розшуку МВС (CarsWanted).</i>")
    lines = ["🚨 <b>АВТО В РОЗШУКУ!</b>\n"]
    for w in wanted:
        lines.append(f"• {w.get('brandmodel') or ''} · {(w.get('color') or '').lower()}")
        lines.append(f"  📆 Заволодіння: {_fmt_date(w.get('seizure'))}")
        if w.get("organ"):
            lines.append(f"  🏢 {w['organ']}")
    lines.append("\n<i>Джерело: відкритий датасет розшуку МВС.</i>")
    return "\n".join(lines)


def _ac_menu_kb(d: dict, query: str) -> InlineKeyboardMarkup:
    """Section buttons (held behind taps) + external official-check links + nav."""
    import re as _re
    from urllib.parse import quote

    v = d.get("vehicle") or {}
    plate = v.get("plate") or ""
    vin = v.get("vin") or ""
    primary = plate or vin or _ac_detect(query)[1]
    q = (query or "").strip()
    alnum = _re.sub(r"[^A-Z0-9]", "", q.upper())
    is_vin = bool(vin) or (bool(_re.search(r"\d", q)) and len(alnum) >= 10
                           and not _re.search(r"[А-ЯІЇЄҐ]", q.upper()))
    found = bool(d.get("found"))
    wanted = bool(d.get("wanted"))
    b = InlineKeyboardBuilder()
    groups: list = []
    # Вільний номер (не в реєстрі) → пропозиція моніторингу доступності.
    if not found and not wanted:
        fp = _full_plate(query)
        if fp:
            b.button(text="🔔 Поставити на моніторинг", callback_data=f"acmon:{fp}")
            groups.append(1)
    # Внутрішні розділи — кожен показується тільки по натисканню (progressive disclosure).
    g = 0
    if found:
        b.button(text="📋 Держреєстрація", callback_data=f"ac:reg:{vin or primary}")
        g += 1
    if found or wanted:
        b.button(text="🚨 Розшук", callback_data=f"ac:roz:{primary}")
        g += 1
    if g:
        groups.append(g)
    g = 0
    if plate:
        b.button(text="🔢 Історія номера", callback_data=f"ac:pl:{plate}")
        g += 1
    if vin:
        b.button(text="🚗 Історія авто", callback_data=f"ac:vin:{vin}")
        g += 1
    if g:
        groups.append(g)
    # Зовнішні офіційні перевірки (відкривають сайт у натиску — як просив Артур).
    ext = 0
    b.button(text="🚗 AutoRia", url=f"https://auto.ria.com/uk/search/?text={quote(q)}")
    ext += 1
    if is_vin:
        b.button(text="🇺🇸 Аукціони (VIN)", url=f"https://en.bidfax.info/?do=search&subaction=search&story={quote(alnum)}")
        ext += 1
    b.button(text="🛡 ОСАГО (МТСБУ)", url="https://policy.mtsbu.ua/")
    ext += 1
    b.button(text="⚖️ Обтяження (Мінюст)", url="https://online.minjust.gov.ua/")
    ext += 1
    groups += [2] * (ext // 2) + ([1] if ext % 2 else [])
    b.button(text="🚗 Перевірити ще", callback_data="acheck")
    b.button(text="⬅️ Меню", callback_data="menu")
    groups.append(2)
    b.adjust(*groups)
    return b.as_markup()


def _car_label(r: dict) -> str:
    """'TOYOTA CAMRY · 2018' from a history row."""
    name = " ".join(str(x) for x in (r.get("brand"), r.get("model")) if x).strip()
    if r.get("make_year"):
        name = (name + f" · {r['make_year']}").strip(" ·")
    return name or "Авто"


def _op_text(oper: Optional[str]) -> str:
    """Tidy an operation name (sentence case, no trailing dot)."""
    s = (oper or "").strip().rstrip(".")
    return (s[:1].upper() + s[1:].lower()) if s else "операція"


def _dedup_ops(rows: list) -> list:
    """Drop exact-duplicate operations (same date+oper+dep+vin)."""
    seen, out = set(), []
    for r in rows:
        k = (r.get("d_reg"), r.get("oper_name"), r.get("dep"), r.get("vin"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _op_line(r: dict, show_plate: bool = False) -> str:
    """One operation as a clean line: date · [plate] · operation · ТСЦ."""
    date = _fmt_date(r.get("d_reg"))
    head = f"🗓 <b>{date}</b>" if date != "—" else "🗓 <i>дата н/д</i>"
    bits = [head]
    if show_plate and r.get("plate"):
        bits.append(f"🔢 {r['plate']}")
    bits.append(_op_text(r.get("oper_name")))
    line = "▪️ " + " · ".join(bits)
    if r.get("dep"):
        line += f"\n     🏢 {r['dep']}"
    return line


def _fmt_ac_history(d: dict, mode: str, key: str) -> str:
    """Render operations grouped for readability. mode: 'plate' | 'vin'."""
    if d.get("offline"):
        return "⏳ База перевірки авто зараз недоступна (агент на ПК вимкнено). Спробуй пізніше."
    h = _dedup_ops(d.get("history") or [])
    cap = 20
    if mode == "plate":
        head = f"🔢 <b>Історія номера {key}</b>"
        if not h:
            return head + "\n\nОперацій не знайдено."
        # групуємо за авто (VIN) — у порядку появи, нові зверху
        order, groups = [], {}
        for r in h:
            gid = r.get("vin") or _car_label(r)
            if gid not in groups:
                groups[gid] = []
                order.append(gid)
            groups[gid].append(r)
        lines = [head, f"Операцій: <b>{len(h)}</b> · авто на номері: <b>{len(order)}</b>"]
        shown = 0
        for gid in order:
            ops = groups[gid]
            lines.append(f"\n🚗 <b>{_car_label(ops[0])}</b>")
            if ops[0].get("vin"):
                lines.append(f"🔑 <code>{ops[0]['vin']}</code>")
            for r in ops:
                if shown >= cap:
                    break
                lines.append(_op_line(r))
                shown += 1
        if len(h) > cap:
            lines.append(f"\n…та ще {len(h) - cap} операцій")
        return "\n".join(lines)
    # mode == 'vin' — одне авто, показуємо номери, що на ньому були
    head = "🚗 <b>Історія авто</b>"
    if not h:
        return head + f"\n<i>VIN {key}</i>\n\nОперацій не знайдено."
    plates, seen = [], set()
    for r in h:
        p = r.get("plate")
        if p and p not in seen:
            seen.add(p)
            plates.append(p)
    lines = [head, f"<b>{_car_label(h[0])}</b>", f"🔑 <code>{key}</code>"]
    if plates:
        lines.append(f"🔢 Номери на авто: <b>{', '.join(plates)}</b>")
    lines.append(f"Операцій: <b>{len(h)}</b>\n")
    for r in h[:cap]:
        lines.append(_op_line(r, show_plate=len(plates) > 1))
    if len(h) > cap:
        lines.append(f"\n…та ще {len(h) - cap} операцій")
    return "\n".join(lines)


def _ac_sub_kb(primary_key: str) -> InlineKeyboardMarkup:
    """Back-to-summary + menu for a section view."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"ac:sum:{primary_key}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2)
    return b.as_markup()


def _ac_vin_kb(vin: str) -> InlineKeyboardMarkup:
    """VIN-history view: deep-link to AutoRia VIN search (free) + back + menu."""
    from urllib.parse import quote

    b = InlineKeyboardBuilder()
    b.button(text="🔎 Цей VIN на AutoRia", url=f"https://auto.ria.com/uk/search/?text={quote(vin)}")
    b.button(text="⬅️ Назад", callback_data=f"ac:sum:{vin}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1, 2)
    return b.as_markup()


@dp.callback_query(F.data.startswith("ac:"))
async def cb_acsec(cq: CallbackQuery, state: FSMContext) -> None:
    """Reveal one AutoCheck section on tap: reg / roz / plate-hist / vin-hist / back to summary."""
    try:
        _, sec, key = cq.data.split(":", 2)
    except ValueError:
        await cq.answer()
        return
    await cq.answer()
    await show(cq.message.bot, cq.message.chat.id, "🔎 Завантажую…", kb_back())
    if sec == "pl":
        res = await _ac_get("plate", key)
        await show(cq.message.bot, cq.message.chat.id, _fmt_ac_history(res, "plate", key), _ac_sub_kb(key))
        return
    if sec == "vin":
        res = await _ac_get("vin", key)
        await show(cq.message.bot, cq.message.chat.id, _fmt_ac_history(res, "vin", key), _ac_vin_kb(key))
        return
    # reg / roz / sum — повний lookup за ключем (поточне авто + розшук)
    param, val = _ac_detect(key)
    res = await _ac_get(param, val)
    if sec == "reg":
        await show(cq.message.bot, cq.message.chat.id, _fmt_ac_reg(res), _ac_sub_kb(key))
    elif sec == "roz":
        await show(cq.message.bot, cq.message.chat.id, _fmt_ac_roz(res), _ac_sub_kb(key))
    else:  # sum — назад до короткого екрану з кнопками
        await show(cq.message.bot, cq.message.chat.id, _fmt_ac_summary(res, key), _ac_menu_kb(res, key))


async def _create_plate_monitor(bot: Bot, chat_id: int, plate: str) -> None:
    """Create an exact-plate availability monitor (hunt) and confirm."""
    from local.plate import parse_plate

    used = await db.active_hunt_count(chat_id)
    limit = await db.hunt_limit(chat_id)
    if used >= limit:
        await show(bot, chat_id,
                   f"⚠️ Ліміт моніторингів вичерпано ({used}/{limit}).\n\n"
                   "👥 Запроси друзів — за кожного +1 слот.",
                   kb_back([("👥 Запросити друзів", "ref"), ("🎯 Мої моніторинги", "myhunts")]))
        return
    p = parse_plate(plate)
    h = {"match_type": "exact", "pattern": plate, "name": plate,
         "letters_start": p.get("letters_start"), "letters_end": p.get("letters_end"),
         "digits_exact": p.get("digits"), "region": None, "vehicle_type": None}
    await db.ensure_user(chat_id, None)
    await db.add_hunt(chat_id, h)
    cnt = await db.count_hunt_matches(h)
    lines = ["✅ <b>Моніторинг створено</b>", f"Номер: <b>{plate}</b>", ""]
    if cnt:
        lines.append("🔎 Цей номер зараз <b>доступний</b> — глянь у пошуку!")
    else:
        lines.append("🔎 Зараз його немає в продажу — сповіщу, щойно зʼявиться.")
    await show(bot, chat_id, "\n".join(lines), kb_back([("🎯 Мої моніторинги", "myhunts")]))


@dp.callback_query(F.data.startswith("acmon:"))
async def cb_acmon(cq: CallbackQuery, state: FSMContext) -> None:
    """Create an availability monitor for a free plate from the AutoCheck result."""
    plate = cq.data.split(":", 1)[1]
    await cq.answer()
    await _create_plate_monitor(cq.message.bot, cq.message.chat.id, plate)


@dp.callback_query(F.data == "acheck")
async def cb_acheck(cq: CallbackQuery, state: FSMContext) -> None:
    """Start the AutoCheck flow — ask for a plate or VIN."""
    await state.set_state(Flow.acheck)
    await show(cq.message.bot, cq.message.chat.id,
              "🚗 <b>Перевірка авто</b>\n\nНадішли <b>номер</b> (напр. АА1234ВН) або <b>VIN</b> — "
              "покажу марку, модель, рік, обʼєм, паливо, колір та <b>історію реєстрацій</b> "
              "з відкритого реєстру МВС.", kb_back())


@dp.message(Flow.acheck)
async def do_acheck(message: Message, state: FSMContext) -> None:
    """Run an AutoCheck lookup for the user's plate/VIN and show the result."""
    q = (message.text or "").strip()
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await state.clear()
    if not q:
        await show(message.bot, message.chat.id, "Порожній запит. Спробуй ще раз.",
                   kb_back([("🚗 Перевірити", "acheck")]))
        return
    await show(message.bot, message.chat.id, "🔎 Шукаю в реєстрі МВС…", kb_back())
    res = await _autocheck_query(q)
    await show(message.bot, message.chat.id, _fmt_ac_summary(res, q), _ac_menu_kb(res, q))


# ── Підбір за комбінацією цифр: доступні / зайняті / вільні (обʼєднання баз) ──
_CGRID = 18


async def _cf(state: FSMContext) -> dict:
    return (await state.get_data()).get("cf", {})


async def _occupied(digits: str, series: Optional[list] = None,
                    regions: Optional[list] = None) -> list:
    """Ask the server which plates with this digit-combo are registered (occupied)."""
    import aiohttp

    params = {"digits": digits}
    if series:
        params["series"] = ",".join(series)
    if regions:
        params["regions"] = ",".join(regions)
    headers = {"X-API-Key": config.API_KEY} if getattr(config, "API_KEY", "") else {}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8000/autocheck/occupied", params=params,
                             headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
                return (await r.json()).get("occupied") or []
    except Exception:  # noqa: BLE001
        return []


def _combo_grid(plates: list, page_cb: str, page: int, has_more: bool) -> InlineKeyboardMarkup:
    """3-per-row plate buttons + pagination + back to categories/menu."""
    b = InlineKeyboardBuilder()
    for p in plates:
        b.button(text=p, callback_data=f"cmb:p:{p}")
    nav = 0
    if page > 0:
        b.button(text="◀️ Назад", callback_data=f"{page_cb}:{page - 1}")
        nav += 1
    if has_more:
        b.button(text="▶️ Далі", callback_data=f"{page_cb}:{page + 1}")
        nav += 1
    b.button(text="⬅️ Розділи", callback_data="cmb:cats")
    b.button(text="⬅️ Меню", callback_data="menu")
    layout = [3] * (len(plates) // 3)
    if len(plates) % 3:
        layout.append(len(plates) % 3)
    if nav:
        layout.append(nav)
    layout.append(2)
    b.adjust(*layout)
    return b.as_markup()


@dp.callback_query(F.data == "combo")
async def cb_combo(cq: CallbackQuery, state: FSMContext) -> None:
    """Entry: ask for a 4-digit combination."""
    await state.set_state(Flow.combo)
    await state.update_data(cf={})
    await show(cq.message.bot, cq.message.chat.id,
              "🔢 <b>Підбір за комбінацією</b>\n━━━━━━━━━━━━\n"
              "Надішли <b>4 цифри</b> номера, напр. <b>0100</b>.\n\n"
              "Покажу: 🟢 доступні зараз, 🔴 зайняті (на авто), ⚪ вільні під полювання.",
              kb_back())


@dp.message(Flow.combo)
async def do_combo(message: Message, state: FSMContext) -> None:
    """Receive the digit combination and show the category screen."""
    import re as _re

    digits = _re.sub(r"\D", "", message.text or "")
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if len(digits) != 4:
        await show(message.bot, message.chat.id,
                   "Потрібно рівно <b>4 цифри</b> (напр. 0100). Спробуй ще:",
                   kb_back([("🔢 Ще раз", "combo")]))
        return
    await state.update_data(cf={"digits": digits})
    await show(message.bot, message.chat.id, "🔎 Збираю дані по обох базах…", kb_back())
    await render_combo_cats(message.bot, message.chat.id, state)


_ALL_SERIES = sorted({e for lst in TYPE_SERIES_OFFICIAL.values() for e in lst})


def _combo_scope(cf: dict):
    """(region, vtype, region_codes|None, series|None) з поточних фільтрів combo."""
    region = cf.get("region")
    vtype = cf.get("vtype")
    codes = REGION_SERIES.get(region) if region else None
    series = TYPE_SERIES_OFFICIAL.get(vtype) if vtype else None
    return region, vtype, codes, series


async def render_combo_cats(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Combo screen: 🟢 в продажу / 🔴 на авто / ⚪ вільні — усі з фільтрами Тип+Регіон."""
    cf = await _cf(state)
    digits = cf.get("digits")
    if not digits:
        await show(bot, chat_id, "Почни заново:", kb_back([("🔢 Комбінація", "combo")]))
        return
    region, vtype, codes, series = _combo_scope(cf)
    await show(bot, chat_id, "🔎 Рахую по обох базах…", kb_back())
    av_total = await db.count_filtered(query=digits, region=region, vehicle_type=vtype)
    okey = f"{region}|{vtype}"
    if cf.get("occ_key") != okey:
        cf["occ"] = await _occupied(digits, series=series, regions=codes)
        cf["occ_key"] = okey
    occ = cf.get("occ") or []
    free_n = None
    if region:  # вільні рахуємо лише коли заданий регіон (інакше всесвіт завеликий)
        ser = series or _ALL_SERIES
        universe = {c + digits + e for c in codes for e in ser}
        av_rows = await db.search_filtered(query=digits, region=region, vehicle_type=vtype, limit=3000)
        av_set = {r["plate_number"] for r in av_rows} & universe
        occ_set = {o["plate"] for o in occ} & universe
        cf["free"] = sorted(universe - av_set - occ_set)
        cf["free_total"] = len(universe)
        free_n = len(cf["free"])
    await state.update_data(cf=cf)
    flt = f"🚗 {vtype or 'всі типи'}  ·  🌍 {region or 'всі регіони'}"
    lines = [f"🔢 <b>Комбінація {digits}</b>", "━━━━━━━━━━━━", f"<i>{flt}</i>", "",
             f"🟢 В продажу: <b>{av_total}</b>", f"🔴 На авто: <b>{len(occ)}</b>",
             (f"⚪ Вільні: <b>{free_n}</b>" if free_n is not None else "⚪ Вільні: <i>обери регіон</i>"),
             "\nОбери розділ або зміни фільтр 👇"]
    b = InlineKeyboardBuilder()
    rows = []
    cat = 0
    if av_total:
        b.button(text=f"🟢 В продажу ({av_total})", callback_data="cmb:av:0")
        cat += 1
    if occ:
        b.button(text=f"🔴 На авто ({len(occ)})", callback_data="cmb:oc:0")
        cat += 1
    if cat:
        rows.append(cat)
    if free_n:
        b.button(text=f"⚪ Вільні ({free_n}) — обрати серію", callback_data="cmb:free")
        rows.append(1)
    b.button(text=f"🚗 Тип: {vtype or 'всі'}", callback_data="cmb:settype")
    b.button(text=f"🌍 Регіон: {region or 'всі'}", callback_data="cmb:setreg")
    rows.append(2)
    b.button(text="🔢 Інша комбінація", callback_data="combo")
    b.button(text="⬅️ Меню", callback_data="menu")
    rows.append(2)
    b.adjust(*rows)
    await show(bot, chat_id, "\n".join(lines), b.as_markup())


@dp.callback_query(F.data == "cmb:cats")
async def cb_cmb_cats(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await render_combo_cats(cq.message.bot, cq.message.chat.id, state)


@dp.callback_query(F.data.startswith("cmb:av:"))
async def cb_cmb_av(cq: CallbackQuery, state: FSMContext) -> None:
    """List currently-available plates for the combo (HSC feed), paginated."""
    page = int(cq.data.split(":")[2])
    await cq.answer()
    cf = await _cf(state)
    digits = cf.get("digits")
    if not digits:
        await show(cq.message.bot, cq.message.chat.id, "Почни заново:", kb_back([("🔢 Комбінація", "combo")]))
        return
    region, vtype, _, _ = _combo_scope(cf)
    total = await db.count_filtered(query=digits, region=region, vehicle_type=vtype)
    rows = await db.search_filtered(query=digits, region=region, vehicle_type=vtype,
                                    limit=_CGRID, offset=page * _CGRID)
    plates = [r["plate_number"] for r in rows]
    has_more = (page + 1) * _CGRID < total
    text = (f"🟢 <b>В продажу</b> · {digits}\n{vtype or 'всі типи'} · {region or 'всі регіони'}\n"
            f"Усього: <b>{total}</b> · стор. {page + 1}\n\nТап → перевірити номер 👇")
    await show(cq.message.bot, cq.message.chat.id, text, _combo_grid(plates, "cmb:av", page, has_more))


@dp.callback_query(F.data.startswith("cmb:oc:"))
async def cb_cmb_oc(cq: CallbackQuery, state: FSMContext) -> None:
    """List occupied (registered) plates for the combo, paginated."""
    page = int(cq.data.split(":")[2])
    await cq.answer()
    cf = await _cf(state)
    occ = cf.get("occ") or []
    start = page * _CGRID
    chunk = occ[start:start + _CGRID]
    plates = [o["plate"] for o in chunk]
    has_more = start + _CGRID < len(occ)
    region, vtype, _, _ = _combo_scope(cf)
    text = (f"🔴 <b>На авто</b> · {cf.get('digits')}\n{vtype or 'всі типи'} · {region or 'всі регіони'}\n"
            f"Усього в реєстрі: <b>{len(occ)}</b> · стор. {page + 1}\n\nТап → дані авто 👇")
    await show(cq.message.bot, cq.message.chat.id, text, _combo_grid(plates, "cmb:oc", page, has_more))


@dp.callback_query(F.data == "cmb:settype")
async def cb_cmb_settype(cq: CallbackQuery, state: FSMContext) -> None:
    """Filter: choose vehicle type (or all)."""
    await cq.answer()
    opts = ["(всі типи)"] + list(TYPE_SERIES_OFFICIAL.keys())
    cf = await _cf(state)
    cf["topts"] = opts
    await state.update_data(cf=cf)
    b = InlineKeyboardBuilder()
    for i, t in enumerate(opts):
        b.button(text=t, callback_data=f"cmb:st:{i}")
    b.button(text="⬅️ Назад", callback_data="cmb:cats")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id, "🚗 <b>Тип ТЗ</b> — обери фільтр:", b.as_markup())


@dp.callback_query(F.data.startswith("cmb:st:"))
async def cb_cmb_st(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    cf = await _cf(state)
    i = int(cq.data.split(":")[2])
    opts = cf.get("topts") or []
    if 0 <= i < len(opts):
        cf["vtype"] = None if i == 0 else opts[i]
        cf["occ_key"] = None  # фільтр змінився → скинути кеш зайнятих
        await state.update_data(cf=cf)
    await render_combo_cats(cq.message.bot, cq.message.chat.id, state)


@dp.callback_query(F.data == "cmb:setreg")
async def cb_cmb_setreg(cq: CallbackQuery, state: FSMContext) -> None:
    """Filter: choose region (or all)."""
    await cq.answer()
    opts = ["(всі регіони)"] + sorted(REGION_SERIES.keys())
    cf = await _cf(state)
    cf["ropts"] = opts
    await state.update_data(cf=cf)
    b = InlineKeyboardBuilder()
    for i, r in enumerate(opts):
        b.button(text=r, callback_data=f"cmb:sr:{i}")
    b.button(text="⬅️ Назад", callback_data="cmb:cats")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id, "🌍 <b>Регіон</b> — обери фільтр:", b.as_markup())


@dp.callback_query(F.data.startswith("cmb:sr:"))
async def cb_cmb_sr(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    cf = await _cf(state)
    i = int(cq.data.split(":")[2])
    opts = cf.get("ropts") or []
    if 0 <= i < len(opts):
        cf["region"] = None if i == 0 else opts[i]
        cf["occ_key"] = None
        await state.update_data(cf=cf)
    await render_combo_cats(cq.message.bot, cq.message.chat.id, state)


@dp.callback_query(F.data == "cmb:free")
async def cb_cmb_free(cq: CallbackQuery, state: FSMContext) -> None:
    """Крок «вибір серії»: спершу серія (код регіону), потім повні номери."""
    await cq.answer()
    cf = await _cf(state)
    region = cf.get("region")
    if not region:  # серія = код регіону, тож регіон треба задати
        await cb_cmb_setreg(cq, state)
        return
    codes = REGION_SERIES.get(region, [])
    b = InlineKeyboardBuilder()
    for c in codes:
        b.button(text=c, callback_data=f"cmb:ps:{c}:0")
    b.button(text="⌨️ Інша серія", callback_data="cmb:psother")
    b.button(text="⬅️ Розділи", callback_data="cmb:cats")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id,
              f"⚪ <b>Вільні</b> · {cf.get('digits')}\n━━━━━━━━━━━━\n{region}\n\n"
              "Обери <b>серію</b> (код регіону) — потім покажу повні вільні номери:", b.as_markup())


async def _render_free_for_prefix(bot: Bot, chat_id: int, state: FSMContext, prefix: str,
                                  page: int = 0) -> None:
    """Повні вільні номери для конкретної серії (префікса): prefix + цифри + кінцеві серії."""
    cf = await _cf(state)
    digits = cf.get("digits")
    if not digits or not prefix:
        await render_combo_cats(bot, chat_id, state)
        return
    vtype = cf.get("vtype")
    if cf.get("free_prefix") != prefix:
        await show(bot, chat_id, "🔎 Рахую вільні номери…", kb_back())
        endings = TYPE_SERIES_OFFICIAL.get(vtype) if vtype else _ALL_SERIES
        universe = {prefix + digits + e for e in endings}
        occ = await _occupied(digits, series=endings, regions=[prefix])
        occ_set = {o["plate"] for o in occ} & universe
        av_rows = await db.search_filtered(query=digits, letters_start=prefix, vehicle_type=vtype, limit=3000)
        av_set = {r["plate_number"] for r in av_rows} & universe
        cf["free"] = sorted(universe - occ_set - av_set)
        cf["free_total"] = len(universe)
        cf["free_av"] = len(av_set)
        cf["free_occ"] = len(occ_set)
        cf["free_prefix"] = prefix
        await state.update_data(cf=cf)
    free = cf.get("free") or []
    start = page * _CGRID
    chunk = free[start:start + _CGRID]
    has_more = start + _CGRID < len(free)
    head = (f"⚪ <b>Серія {prefix}</b> · {digits} · {vtype or 'всі типи'}\n━━━━━━━━━━━━\n"
            f"Усього номерів: <b>{cf.get('free_total', 0)}</b>\n"
            f"🟢 В продажу: <b>{cf.get('free_av', 0)}</b> · 🔴 На авто: <b>{cf.get('free_occ', 0)}</b> · "
            f"⚪ Вільні: <b>{len(free)}</b>\n")
    if not free:
        await show(bot, chat_id, head + "\nУсі номери цієї серії — або в продажу, або на авто.",
                   kb_back([("⬅️ Інша серія", "cmb:free"), ("⬅️ Розділи", "cmb:cats")]))
        return
    await show(bot, chat_id, head + f"\n⚪ Тап → постав полювання · стор. {page + 1} 👇",
               _combo_grid(chunk, f"cmb:ps:{prefix}", page, has_more))


@dp.callback_query(F.data.startswith("cmb:ps:"))
async def cb_cmb_ps(cq: CallbackQuery, state: FSMContext) -> None:
    """Picked a series prefix → show its full free numbers (paginated)."""
    parts = cq.data.split(":")
    prefix, page = parts[2], int(parts[3])
    await cq.answer()
    await _render_free_for_prefix(cq.message.bot, cq.message.chat.id, state, prefix, page)


@dp.callback_query(F.data == "cmb:psother")
async def cb_cmb_psother(cq: CallbackQuery, state: FSMContext) -> None:
    """Type a custom 2-letter series (for future/extra region codes)."""
    await cq.answer()
    await state.set_state(Flow.combo_series)
    await show(cq.message.bot, cq.message.chat.id,
              "⌨️ Введи <b>серію</b> — 2 літери коду регіону (напр. <b>АВ</b>):", kb_back())


@dp.message(Flow.combo_series)
async def do_combo_series(message: Message, state: FSMContext) -> None:
    """Receive a custom series prefix and show its free numbers."""
    from local.plate import normalize_plate

    raw = normalize_plate(message.text or "")
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    import re as _re
    if not _re.fullmatch(r"[A-ZА-ЯІЇЄҐ]{2}", raw):
        await show(message.bot, message.chat.id, "Потрібно рівно 2 літери (напр. АВ). Спробуй ще:",
                   kb_back([("⌨️ Ще раз", "cmb:psother")]))
        return
    await state.set_state(Flow.combo)
    await _render_free_for_prefix(message.bot, message.chat.id, state, raw, 0)


@dp.callback_query(F.data.startswith("cmb:p:"))
async def cb_cmb_pick(cq: CallbackQuery, state: FSMContext) -> None:
    """Tap a specific plate → full status via AutoCheck (car data, or free → monitor)."""
    plate = cq.data.split(":", 2)[2]
    await cq.answer()
    await show(cq.message.bot, cq.message.chat.id, "🔎 Перевіряю номер…", kb_back())
    res = await _autocheck_query(plate)
    await show(cq.message.bot, cq.message.chat.id, _fmt_ac_summary(res, plate), _ac_menu_kb(res, plate))


# ── Слово на номері (пошук по перших + останніх літерах) ──
_WORD_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                               "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})


@dp.callback_query(F.data == "wordsearch")
async def cb_wordsearch(cq: CallbackQuery, state: FSMContext) -> None:
    """Entry for «Слово на номері» — choose step-by-step builder or free-text mask."""
    await state.set_state(None)
    b = InlineKeyboardBuilder()
    b.button(text="📋 Зібрати покроково", callback_data="word_wizard")
    b.button(text="✍️ Написати маску", callback_data="word_type")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id,
              "🔤 <b>Слово на номері</b>\n\nСклади номер-слово: перші літери (регіон) + останні (серія).\n\n"
              "• <b>Зібрати покроково</b> — тип → регіон → код регіону → серія (обираєш кнопками)\n"
              "• <b>Написати маску</b> — напр. <code>СЕ****КС</code> або <code>СЕКС</code>",
              b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "word_wizard")
async def cb_word_wizard(cq: CallbackQuery, state: FSMContext) -> None:
    """Step-by-step word builder — reuse the search wizard (type→region→код→серія→…)."""
    await state.set_state(Flow.search)
    await _set_filters(state, {"mode": "search"})
    await render_step(cq.message.bot, cq.message.chat.id, state, "type")
    await cq.answer()


@dp.callback_query(F.data == "word_type")
async def cb_word_type(cq: CallbackQuery, state: FSMContext) -> None:
    """Free-text mask entry for the word search."""
    await state.set_state(Flow.word)
    await show(cq.message.bot, cq.message.chat.id,
              "✍️ <b>Слово на номері</b>\n\nНадішли 2 перші + 2 останні літери, що складають слово — "
              "напр. <b>СЕ****КС</b>, <b>ВО****РР</b> (або просто <code>СЕКС</code>).\n\n"
              "Перші 2 — код регіону, останні 2 — серія. Покажу доступні; якщо нема — можна "
              "поставити моніторинг наперед.", kb_back())
    await cq.answer()


@dp.message(Flow.word)
async def do_word(message: Message, state: FSMContext) -> None:
    """Parse front+back letters and run a combination search (reuses the results renderer)."""
    import re as _re

    raw = message.text or ""
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    letters = _re.sub(r"[^A-Za-zА-Яа-яІЇЄҐіїєґ]", "", raw).upper().translate(_WORD_LAT2CYR)
    if len(letters) < 4:
        await state.clear()
        await show(message.bot, message.chat.id,
                  "✋ Треба щонайменше 4 літери: 2 перші + 2 останні (напр. СЕКС або СЕ****КС).",
                  kb_back([("🔤 Спробувати ще", "wordsearch")]))
        return
    front, back = letters[:2], letters[-2:]
    await state.clear()
    await _set_filters(state, {"mode": "search", "series": front, "series_end": back, "page": 0})
    await _finalize(message.bot, message.chat.id, state)


@dp.callback_query(F.data == "m_search")
async def cb_m_search(cq: CallbackQuery, state: FSMContext) -> None:
    """Section: search."""
    await state.clear()
    await show(cq.message.bot, cq.message.chat.id,
              "🔍 <b>Пошук номера</b>\n━━━━━━━━━━━━\n<i>Знайди вільний номер для реєстрації</i>",
              _kb_submenu([("🔢 Підбір за комбінацією", "combo"), ("🎛 Розширений пошук", "search"),
                           ("🔤 Слово на номері", "wordsearch"),
                           ("✨ Добірки", "cols"), ("🔥 Популярні", "popular")]))


@dp.callback_query(F.data == "m_monitor")
async def cb_m_monitor(cq: CallbackQuery, state: FSMContext) -> None:
    """Section: monitoring."""
    await state.clear()
    await show(cq.message.bot, cq.message.chat.id,
              "🔔 <b>Моніторинг</b>\n━━━━━━━━━━━━\n<i>Стеж за номером — сповіщу, щойно зʼявиться</i>",
              _kb_submenu([("🔔 Стежити за номером", "newhunt"), ("🎯 Мої моніторинги", "myhunts"),
                           ("📰 Нові / зниклі", "feed"), ("⭐ Обрані", "favs")]))


@dp.callback_query(F.data == "m_more")
async def cb_m_more(cq: CallbackQuery, state: FSMContext) -> None:
    """Section: account / extras."""
    await state.clear()
    await show(cq.message.bot, cq.message.chat.id,
              "⚙️ <b>Ще</b>\n━━━━━━━━━━━━\n<i>Акаунт, статистика, довідка</i>",
              _kb_submenu([("💎 Тариф", "plan"), ("👥 Друзі", "ref"),
                           ("📊 Статистика", "stats"), ("ℹ️ Довідка", "help")]))


@dp.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery, state: FSMContext) -> None:
    """Return to the main menu."""
    await state.clear()
    await render_main(cq.message.bot, cq.message.chat.id)
    await cq.answer()


@dp.callback_query(F.data == "help")
async def cb_help(cq: CallbackQuery) -> None:
    """Show help."""
    text = (
        "ℹ️ <b>Довідка</b>\n\n"
        "🔍 <b>Пошук</b> — покроково: тип → регіон → ціна → ТСЦ → номер\n"
        "Маска цифр у номері: <code>-</code> або <code>*</code> = будь-яка цифра\n"
        "<code>1**4</code> — 1-ша 1, 4-та 4 · <code>12--</code> — починається на 12\n\n"
        "🎯 <b>Моніторинги</b> — шаблони, за якими сповіщу, щойно номер зʼявиться:\n"
        "<code>АА****</code>, <code>****ВВ</code>, <code>АА****ВВ</code>, <code>1234</code>, <code>АА1234ВВ</code>"
    )
    await show(cq.message.bot, cq.message.chat.id, text, kb_back())
    await cq.answer()


def _ref_link(chat_id: int) -> str:
    """Build the user's referral deep-link."""
    return f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"


@dp.callback_query(F.data == "ref")
async def cb_ref(cq: CallbackQuery) -> None:
    """Referral screen: link, progress, rewards."""
    chat_id = cq.message.chat.id
    user = await db.get_user(chat_id) or {}
    link = _ref_link(chat_id)
    invited = user.get("invited_count") or 0
    limit = await db.hunt_limit(chat_id)
    need_pro = max(0, db.PRO_INVITE_THRESHOLD - invited)
    progress = (f"Ще <b>{need_pro}</b> друзів → 💎 PRO на {db.PRO_DAYS_FOR_INVITES} днів"
                if need_pro else "💎 Ти вже отримав PRO за запрошення!")
    phone_txt = "✅ номер телефону надано (+бонус отримано)" if user.get("shared") \
        else f"📱 поділись номером телефону → +{db.SHARE_BONUS_HUNTS} моніторинги (одноразово)"
    bot_share = f"https://t.me/share/url?url={quote(link)}&text={quote('Знайди свій ідеальний автономер у боті 🚗')}"
    text = (
        "👥 <b>Запроси друзів — отримай моніторинги</b>\n\n"
        f"• {phone_txt}\n"
        f"• за кожні {db.FRIENDS_PER_HUNT} друзі → +1 моніторинг\n"
        f"• {db.PRO_INVITE_THRESHOLD} друзів → 💎 PRO (безліміт)\n\n"
        f"Запрошено: <b>{invited}</b> · твій ліміт моніторингів: <b>{limit}</b>\n"
        f"{progress}\n\n"
        f"Твоє посилання:\n{link}"
    )
    b = InlineKeyboardBuilder()
    b.button(text="📤 Поділитися ботом", url=bot_share)
    if not user.get("shared"):
        b.button(text=f"📱 Поділитися своїм номером (+{db.SHARE_BONUS_HUNTS})", callback_data="reqphone")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(cq.message.bot, chat_id, text, b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "reqphone")
async def cb_reqphone(cq: CallbackQuery) -> None:
    """Ask the user to share their phone number via a request_contact button."""
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поділитися номером телефону", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await cq.message.bot.send_message(
        cq.message.chat.id,
        f"📱 Натисни кнопку нижче, щоб поділитися своїм номером телефону "
        f"і отримати +{db.SHARE_BONUS_HUNTS} моніторинги.",
        reply_markup=kb,
    )
    await cq.answer()


@dp.message(F.contact)
async def on_contact(message: Message) -> None:
    """Store the shared phone, grant the one-time bonus, remove the reply keyboard."""
    phone = message.contact.phone_number if message.contact else None
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if not phone:
        return
    granted = await db.set_phone(message.chat.id, phone)
    note = (f"✅ Дякую! Номер збережено, +{db.SHARE_BONUS_HUNTS} моніторинги нараховано 🎯"
            if granted else "✅ Номер оновлено (бонус уже було отримано).")
    # Remove the reply keyboard with a throwaway message, then refresh the screen.
    tmp = await message.bot.send_message(message.chat.id, note, reply_markup=ReplyKeyboardRemove())
    await _safe_delete(message.bot, message.chat.id, tmp.message_id)
    _screens.pop(message.chat.id, None)
    await render_main(message.bot, message.chat.id)


@dp.callback_query(F.data == "plan")
async def cb_plan(cq: CallbackQuery) -> None:
    """Tariff screen: current plan, limits, PRO benefits, how to get it."""
    chat_id = cq.message.chat.id
    user = await db.get_user(chat_id) or {}
    pro = db.is_pro(user)
    limit = await db.hunt_limit(chat_id)
    used = await db.active_hunt_count(chat_id)
    lines = ["💎 <b>Тариф</b>\n"]
    if pro:
        lines.append(f"Поточний: <b>PRO</b> до {str(user.get('plan_until'))[:10]}")
        lines.append(f"Моніторинги: {used} / ∞")
    else:
        lines.append("Поточний: <b>FREE</b>")
        lines.append(f"Моніторинги: {used} / {limit}")
    lines.append("\n<b>PRO дає:</b>\n• Безліміт моніторингів\n• Миттєві сповіщення\n• Розширена статистика\n• Експорт CSV\n• Пріоритетна підтримка")
    lines.append(f"\n🎁 Безкоштовно: запроси {db.PRO_INVITE_THRESHOLD} друзів → {db.PRO_DAYS_FOR_INVITES} днів PRO.")
    lines.append(f"\n💎 Або оплати зірками Telegram:")
    b = InlineKeyboardBuilder()
    b.button(text=f"⭐ PRO 1 міс — {PRO_STARS_MONTH}", callback_data="buy:month")
    b.button(text=f"⭐ PRO 1 рік — {PRO_STARS_YEAR}", callback_data="buy:year")
    b.button(text="💳 Картка — скоро", callback_data="card_soon")
    b.button(text="👥 Запросити друзів", callback_data="ref")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2, 1, 1, 1)
    await show(cq.message.bot, chat_id, "\n".join(lines), b.as_markup())
    await cq.answer()


# Telegram Stars (XTR) pricing — adjust freely.
PRO_STARS_MONTH = 100
PRO_STARS_YEAR = 900
_STARS_PLAN_DAYS = {"month": 30, "year": 365}


@dp.callback_query(F.data == "card_soon")
async def cb_card_soon(cq: CallbackQuery) -> None:
    """Card payment placeholder."""
    await cq.answer("Оплата карткою — скоро 💳", show_alert=True)


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(cq: CallbackQuery) -> None:
    """Send a Telegram Stars invoice for a PRO plan."""
    plan = cq.data.split(":", 1)[1]
    stars = PRO_STARS_MONTH if plan == "month" else PRO_STARS_YEAR
    days = _STARS_PLAN_DAYS.get(plan, 30)
    title = f"PRO на {days} днів"
    await cq.message.bot.send_invoice(
        chat_id=cq.message.chat.id,
        title=title,
        description="💎 PRO: безліміт моніторингів, миттєві сповіщення, розширена статистика, експорт.",
        payload=f"pro_{plan}",
        provider_token="",          # empty for Telegram Stars
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )
    await cq.answer()


@dp.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery) -> None:
    """Approve the checkout (required within 10s)."""
    await pcq.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    """Grant PRO after a successful Stars payment."""
    sp = message.successful_payment
    plan = (sp.invoice_payload or "").replace("pro_", "")
    days = _STARS_PLAN_DAYS.get(plan, 30)
    await db.grant_pro(message.chat.id, days)
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    _screens.pop(message.chat.id, None)
    await message.bot.send_message(
        message.chat.id,
        f"🎉 Дякуємо за оплату! 💎 PRO активовано на {days} днів.\nЗірок сплачено: {sp.total_amount} ⭐",
    )
    await render_main(message.bot, message.chat.id)


@dp.callback_query(F.data == "cols")
async def cb_collections(cq: CallbackQuery) -> None:
    """Show curated collections of beautiful numbers."""
    b = InlineKeyboardBuilder()
    for kind, label in db.COLLECTIONS.items():
        b.button(text=label, callback_data=f"col:{kind}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id,
              "✨ <b>Добірки красивих номерів</b>\nОбери категорію:", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("col:"))
async def cb_col(cq: CallbackQuery, state: FSMContext) -> None:
    """Open a collection — first pick Type → Region, then show results."""
    kind = cq.data.split(":", 1)[1]
    await state.set_state(Flow.search)
    await _set_filters(state, {"mode": "collection", "collection": kind})
    await render_step(cq.message.bot, cq.message.chat.id, state, "type")
    await cq.answer()


@dp.callback_query(F.data == "popular")
async def cb_popular(cq: CallbackQuery) -> None:
    """Show the most-favorited digit combinations."""
    combos = await db.popular_combos(10)
    if not combos:
        await show(cq.message.bot, cq.message.chat.id,
                   "🔥 <b>Популярні комбінації</b>\n\nПоки немає даних — додавай номери в ⭐ Обрані.",
                   kb_back([("🔍 Пошук", "search")]))
        await cq.answer()
        return
    lines = ["🔥 <b>Популярні комбінації</b>", "<i>за додаваннями в обране</i>\n"]
    b = InlineKeyboardBuilder()
    for i, c in enumerate(combos, 1):
        lines.append(f"{i}. <b>{c['digits']}</b> · ⭐ {c['c']}")
        b.button(text=f"{c['digits']} ⭐{c['c']}", callback_data=f"pc:{c['digits']}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id, "\n".join(lines), b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("pc:"))
async def cb_popcombo(cq: CallbackQuery, state: FSMContext) -> None:
    """Search a popular combination's digits."""
    digits = cq.data.split(":", 1)[1]
    await state.set_state(Flow.search)
    await _set_filters(state, {"mode": "search", "query": digits, "page": 0})
    await render_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


_PERIOD_LABEL = {"day": "за добу", "week": "за тиждень", "month": "за місяць"}


async def render_feed(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Feed hub: toggle new/removed, period, type, region."""
    f = await _filters(state)
    kind, period = f.get("feed_kind", "new"), f.get("feed_period", "day")
    cnt = await db.feed_count(kind, period, f.get("region"), f.get("vtype"))
    kl = "🆕 Нові" if kind == "new" else "❌ Зниклі"
    text = (f"📰 <b>Стрічка</b>\n{kl} · {_PERIOD_LABEL[period]}\n"
            f"🚗 {f.get('vtype') or 'всі'} · 🌍 {f.get('region') or 'всі'}\n\n"
            f"Знайдено: <b>{cnt:,}</b>".replace(",", " "))
    b = InlineKeyboardBuilder()
    b.button(text="🆕 Нові ✅" if kind == "new" else "🆕 Нові", callback_data="fk:new")
    b.button(text="❌ Зниклі ✅" if kind == "removed" else "❌ Зниклі", callback_data="fk:removed")
    for p, lab in (("day", "Доба"), ("week", "Тиждень"), ("month", "Місяць")):
        b.button(text=f"{lab} ✅" if period == p else lab, callback_data=f"fp:{p}")
    b.button(text=f"🚗 {f.get('vtype') or 'тип'}", callback_data="f_type")
    b.button(text=f"🌍 {f.get('region') or 'регіон'}", callback_data="f_region")
    b.button(text=f"🔎 Показати ({min(cnt, _GRID)})", callback_data="f_show")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2, 3, 2, 1, 1)
    await show(bot, chat_id, text, b.as_markup())


@dp.callback_query(F.data == "feed")
async def cb_feed(cq: CallbackQuery, state: FSMContext) -> None:
    """Open the feed hub (fresh context)."""
    await _set_filters(state, {"feed_kind": "new", "feed_period": "day", "feed_page": 0})
    await render_feed(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data.startswith("fk:"))
async def cb_feed_kind(cq: CallbackQuery, state: FSMContext) -> None:
    """Toggle new/removed."""
    f = await _filters(state)
    f["feed_kind"] = cq.data.split(":", 1)[1]
    f["feed_page"] = 0
    await _set_filters(state, f)
    await render_feed(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data.startswith("fp:"))
async def cb_feed_period(cq: CallbackQuery, state: FSMContext) -> None:
    """Set the feed period."""
    f = await _filters(state)
    f["feed_period"] = cq.data.split(":", 1)[1]
    f["feed_page"] = 0
    await _set_filters(state, f)
    await render_feed(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "f_type")
async def cb_feed_type(cq: CallbackQuery) -> None:
    """Feed type picker."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Будь-який тип", callback_data="fty:__all__")
    for i, t in enumerate(await db.distinct_vehicle_types()):
        b.button(text=t, callback_data=f"fty:{i}")
    b.button(text="⬅️ До стрічки", callback_data="feed_back")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id, "🚗 Тип ТЗ для стрічки:", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("fty:"))
async def cb_feed_setty(cq: CallbackQuery, state: FSMContext) -> None:
    """Set feed vehicle type."""
    v = cq.data.split(":", 1)[1]
    f = await _filters(state)
    if v == "__all__":
        f["vtype"] = None
    else:
        types = await db.distinct_vehicle_types()
        f["vtype"] = types[int(v)] if v.isdigit() and int(v) < len(types) else None
    await _set_filters(state, f)
    await render_feed(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "f_region")
async def cb_feed_region(cq: CallbackQuery) -> None:
    """Feed region picker."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Всі регіони", callback_data="fr:__all__")
    regions = await db.distinct_regions()
    for r in regions:
        b.button(text=r, callback_data=f"fr:{r}")
    b.button(text="⬅️ До стрічки", callback_data="feed_back")
    b.adjust(*([1] + [3] * ((len(regions) + 2) // 3) + [1]))
    await show(cq.message.bot, cq.message.chat.id, "🌍 Регіон для стрічки:", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("fr:"))
async def cb_feed_setr(cq: CallbackQuery, state: FSMContext) -> None:
    """Set feed region."""
    v = cq.data.split(":", 1)[1]
    f = await _filters(state)
    f["region"] = None if v == "__all__" else v
    await _set_filters(state, f)
    await render_feed(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "feed_back")
async def cb_feed_back(cq: CallbackQuery, state: FSMContext) -> None:
    """Back to the feed hub."""
    await render_feed(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


async def render_feed_results(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """List feed items (with date/time), paginated."""
    f = await _filters(state)
    kind, period, page = f.get("feed_kind", "new"), f.get("feed_period", "day"), f.get("feed_page", 0)
    cnt = await db.feed_count(kind, period, f.get("region"), f.get("vtype"))
    rows = await db.feed(kind, period, f.get("region"), f.get("vtype"), limit=_GRID, offset=page * _GRID)
    kl = "🆕 Нові" if kind == "new" else "❌ Зниклі"
    if not rows:
        await show(bot, chat_id, f"📰 {kl} · {_PERIOD_LABEL[period]}\n\nНемає записів за цей період.",
                   kb_back([("📰 Стрічка", "feed")]))
        return
    pages_total = (cnt + _GRID - 1) // _GRID
    text = (f"📰 <b>{kl}</b> · {_PERIOD_LABEL[period]} · {cnt} · стор. {page + 1}/{pages_total}\n\n"
            "Обери номер 👇")
    b = InlineKeyboardBuilder()
    for r in rows:  # номери як кнопки, 3 в рядок → тап відкриває картку
        b.button(text=r["plate_number"], callback_data=f"pdfeed:{r['plate_number']}")
    has_prev, has_next = page > 0, page * _GRID + len(rows) < cnt
    if has_prev:
        b.button(text="◀️ Назад", callback_data="fpg:prev")
    if has_next:
        b.button(text="➡️ Далі", callback_data="fpg:next")
    b.button(text="📰 Фільтри стрічки", callback_data="feed_back")
    b.button(text="⬅️ Меню", callback_data="menu")
    layout = [3] * (len(rows) // 3)
    if len(rows) % 3:
        layout.append(len(rows) % 3)
    nav = int(has_prev) + int(has_next)
    if nav:
        layout.append(nav)
    layout += [2]
    b.adjust(*layout)
    await show(bot, chat_id, text, b.as_markup())


@dp.callback_query(F.data == "f_show")
async def cb_feed_show(cq: CallbackQuery, state: FSMContext) -> None:
    """Show feed results."""
    f = await _filters(state)
    f["feed_page"] = 0
    await _set_filters(state, f)
    await render_feed_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "fpg:next")
async def cb_feed_next(cq: CallbackQuery, state: FSMContext) -> None:
    """Next feed page."""
    f = await _filters(state)
    f["feed_page"] = f.get("feed_page", 0) + 1
    await _set_filters(state, f)
    await render_feed_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "fpg:prev")
async def cb_feed_prev(cq: CallbackQuery, state: FSMContext) -> None:
    """Previous feed page."""
    f = await _filters(state)
    f["feed_page"] = max(0, f.get("feed_page", 0) - 1)
    await _set_filters(state, f)
    await render_feed_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "fd_details")
async def cb_feed_details(cq: CallbackQuery, state: FSMContext) -> None:
    """Buttons per feed-result plate to open details."""
    f = await _filters(state)
    page = f.get("feed_page", 0)
    rows = await db.feed(f.get("feed_kind", "new"), f.get("feed_period", "day"),
                         f.get("region"), f.get("vtype"), limit=_PAGE, offset=page * _PAGE)
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=r["plate_number"], callback_data=f"pdfeed:{r['plate_number']}")
    b.button(text="⬅️ До стрічки", callback_data="f_show")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id, "🔎 <b>Обери номер</b> для деталей:", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("pdfeed:"))
async def cb_plate_detail_feed(cq: CallbackQuery, state: FSMContext) -> None:
    """Open plate details from the feed."""
    f = await _filters(state)
    f["dorigin"] = "feed"
    await _set_filters(state, f)
    await render_detail(cq.message.bot, cq.message.chat.id, state, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data == "stats")
async def cb_stats(cq: CallbackQuery) -> None:
    """Show meaningful statistics."""
    s = await db.get_stats()
    new_day = await db.feed_count("new", "day")
    removed_day = await db.feed_count("removed", "day")
    fmt = lambda v: f"{int(v):,}".replace(",", " ")

    def price_rng() -> str:
        lo, hi = s.get("price_min"), s.get("price_max")
        if lo is None or hi is None:
            return "—"
        return f"{fmt(lo)} – {fmt(hi)} грн"

    top_region = "\n".join(f"  • {r['region']}: {fmt(r['c'])}" for r in s.get("by_region", [])[:6])
    by_type = "\n".join(f"  • {r['t']}: {fmt(r['c'])}" for r in s.get("by_type", [])[:7])
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"🚗 Усього номерів: <b>{fmt(s.get('total') or 0)}</b>\n"
        f"✅ Доступно зараз: <b>{fmt(s.get('available') or 0)}</b>\n"
        f"🆕 Нових за добу: <b>{new_day}</b>\n"
        f"❌ Зникло за добу: <b>{removed_day}</b>\n"
        f"💰 Діапазон цін: <b>{price_rng()}</b>\n\n"
        f"🏆 <b>Топ регіонів:</b>\n{top_region or '  —'}\n\n"
        f"🚘 <b>За типом ТЗ:</b>\n{by_type or '  —'}"
    )
    await show(cq.message.bot, cq.message.chat.id, text, kb_back())
    await cq.answer()


# ── search wizard ─────────────────────────────────
async def render_step(bot: Bot, chat_id: int, state: FSMContext, step: str) -> None:
    """Render one wizard step (type/region/price/tsc/combo)."""
    f = await _filters(state)
    f["step"] = step
    await _set_filters(state, f)
    mode = f.get("mode", "search")
    n = STEP_ORDER.index(step) + 1
    if mode == "collection":
        title = db.COLLECTIONS.get(f.get("collection"), "Добірка")
        head = f"✨ <b>{title}</b> · крок {n}/2\n🚗 {f.get('vtype') or '—'}\n\n"
    else:
        title = "Новий моніторинг" if mode == "hunt" else "Пошук"
        head = (f"{'➕' if mode == 'hunt' else '🔍'} <b>{title}</b> · крок {n}/{len(STEP_ORDER)}\n"
                f"<i>{_summary(f)}</i>\n\n")
    b = InlineKeyboardBuilder()

    if step == "type":
        types = await db.distinct_vehicle_types()
        b.button(text="✅ Будь-який тип", callback_data="setty:__all__")
        for i, t in enumerate(types):
            b.button(text=t, callback_data=f"setty:{i}")
        b.button(text="⬅️ Меню", callback_data="menu")
        b.button(text="➡️ Далі", callback_data="setty:__all__")
        b.adjust(*([1] * (len(types) + 1) + [2]))
        await show(bot, chat_id, head + "🚗 Обери <b>тип ТЗ</b>:", b.as_markup())

    elif step == "region":
        regions = await db.distinct_regions()
        b.button(text="✅ Всі регіони", callback_data="setr:__all__")
        for r in regions:
            b.button(text=r, callback_data=f"setr:{r}")
        b.button(text="⬅️ Назад", callback_data="step:type")
        b.button(text="➡️ Далі", callback_data="setr:__all__")
        b.button(text="⬅️ Меню", callback_data="menu")
        rows = [1] + [3] * ((len(regions) + 2) // 3) + [2, 1]
        b.adjust(*rows)
        await show(bot, chat_id, head + "🌍 Обери <b>регіон</b>:", b.as_markup())

    elif step == "price":
        prices = await db.distinct_prices(
            region=f.get("region"), tsc=f.get("tsc"), vehicle_type=f.get("vtype")
        )
        b.button(text="✅ Будь-яка ціна", callback_data="setpf:__all__")
        for p in prices:
            label = f"{int(p):,} грн".replace(",", " ")
            b.button(text=label, callback_data=f"setpf:{int(p)}")
        b.button(text="⬅️ Назад", callback_data="step:endseries")
        b.button(text="➡️ Далі", callback_data="setpf:__all__")
        b.button(text="⬅️ Меню", callback_data="menu")
        rows = [1] + [2] * ((len(prices) + 1) // 2) + [2, 1]
        b.adjust(*rows)
        note = "💰 Обери <b>ціну</b>:" if prices else "💰 Цін для цих фільтрів немає"
        await show(bot, chat_id, head + note, b.as_markup())

    elif step == "series":
        region = f.get("region")
        if not region:  # код регіону region-specific → skip to the ending-series step
            await render_step(bot, chat_id, state, "endseries")
            return
        available = set(await db.distinct_series(region=region, vehicle_type=f.get("vtype")))
        official = _region_series(region)
        # All official series for the region first, then any extra available ones not in the list.
        ordered = official + [s for s in sorted(available) if s not in official]
        if not ordered:
            ordered = sorted(available)
        b.button(text="✅ Будь-який код регіону", callback_data="sets:__all__")
        for s in ordered:
            mark = "" if s in available else " 🔔"  # 🔔 = немає зараз → лише моніторинг
            b.button(text=f"{s}{mark}", callback_data=f"sets:{s}")
        b.button(text="⬅️ Назад", callback_data="step:region")
        b.button(text="➡️ Далі", callback_data="sets:__all__")
        b.button(text="⬅️ Меню", callback_data="menu")
        rows = [1] + [3] * ((len(ordered) + 2) // 3) + [2, 1]
        b.adjust(*rows)
        note = ("🔤 Обери <b>код регіону</b> (перші 2 літери; 🔔 — поки немає в продажу, можна "
                "поставити моніторинг):" if ordered else "🔤 Кодів не знайдено")
        await show(bot, chat_id, head + note, b.as_markup())

    elif step == "endseries":
        region = f.get("region")
        ends = await db.distinct_series_end(
            region=region, vehicle_type=f.get("vtype"), letters_start=f.get("series"))
        b.button(text="✅ Будь-яка серія", callback_data="sete:__all__")
        for s in ends:
            b.button(text=s, callback_data=f"sete:{s}")
        b.button(text="⌨️ Ввести свою", callback_data="sete:__type__")
        b.button(text="⬅️ Назад", callback_data="step:series" if region else "step:region")
        b.button(text="➡️ Далі", callback_data="sete:__all__")
        b.button(text="⬅️ Меню", callback_data="menu")
        rows = [1] + [3] * ((len(ends) + 2) // 3) + [1, 2, 1]
        b.adjust(*rows)
        note = ("🔡 Обери <b>серію</b> — останні 2 літери (щоб скласти слово на номері), "
                "або введи свою:" if ends
                else "🔡 Готових серій нема — натисни «⌨️ Ввести свою», щоб задати останні 2 літери:")
        await show(bot, chat_id, head + note, b.as_markup())

    elif step == "combo":
        await state.set_state(Flow.search)
        mode = f.get("mode", "search")
        skip_label = "⏭ Пропустити → зберегти моніторинг" if mode == "hunt" else "⏭ Пропустити → показати"
        action = "збережу моніторинг на ці фільтри" if mode == "hunt" else "покажу результати"
        text = (
            head + f"⌨️ Надішли <b>цифри або маску</b> (необовʼязково — інакше {action}):\n"
            "<code>1**4</code> · <code>12--</code> · <code>**34</code> · <code>1234</code>\n"
            "<code>-</code>/<code>*</code> = будь-яка цифра"
        )
        b.button(text=skip_label, callback_data="s_skip")
        b.button(text="⬅️ Назад", callback_data="step:price")
        b.button(text="⬅️ Меню", callback_data="menu")
        b.adjust(1)
        await show(bot, chat_id, text, b.as_markup())


@dp.callback_query(F.data == "search")
async def cb_search(cq: CallbackQuery, state: FSMContext) -> None:
    """Start the wizard in SEARCH mode."""
    await state.set_state(Flow.search)
    await _set_filters(state, {"mode": "search"})
    await render_step(cq.message.bot, cq.message.chat.id, state, "type")
    await cq.answer()


@dp.callback_query(F.data.startswith("step:"))
async def cb_step(cq: CallbackQuery, state: FSMContext) -> None:
    """Navigate to a specific wizard step (back buttons)."""
    await render_step(cq.message.bot, cq.message.chat.id, state, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("setty:"))
async def cb_set_type(cq: CallbackQuery, state: FSMContext) -> None:
    """Pick vehicle type → advance to region."""
    value = cq.data.split(":", 1)[1]
    f = await _filters(state)
    if value == "__all__":
        f["vtype"] = None
    else:
        types = await db.distinct_vehicle_types()
        idx = int(value)
        f["vtype"] = types[idx] if 0 <= idx < len(types) else None
    await _set_filters(state, f)
    await render_step(cq.message.bot, cq.message.chat.id, state, "region")
    await cq.answer()


@dp.callback_query(F.data.startswith("setr:"))
async def cb_set_region(cq: CallbackQuery, state: FSMContext) -> None:
    """Pick region (reset series) → advance to series."""
    value = cq.data.split(":", 1)[1]
    f = await _filters(state)
    f["region"] = None if value == "__all__" else value
    f["series"] = None
    f["series_end"] = None
    await _set_filters(state, f)
    if f.get("mode") == "collection":
        # Collections only filter by type + region → straight to results.
        f["page"] = 0
        await _set_filters(state, f)
        await render_results(cq.message.bot, cq.message.chat.id, state)
    elif f.get("region"):
        # Код регіону region-specific → offer it for a concrete region.
        await render_step(cq.message.bot, cq.message.chat.id, state, "series")
    else:
        await render_step(cq.message.bot, cq.message.chat.id, state, "endseries")
    await cq.answer()


@dp.callback_query(F.data.startswith("sets:"))
async def cb_set_series(cq: CallbackQuery, state: FSMContext) -> None:
    """Pick a region code (first letters) → advance to the ending-series step."""
    value = cq.data.split(":", 1)[1]
    f = await _filters(state)
    f["series"] = None if value == "__all__" else value
    await _set_filters(state, f)
    await render_step(cq.message.bot, cq.message.chat.id, state, "endseries")
    await cq.answer()


@dp.callback_query(F.data.startswith("sete:"))
async def cb_set_endseries(cq: CallbackQuery, state: FSMContext) -> None:
    """Pick the ending series (last letters) → advance to price; or prompt to type it."""
    value = cq.data.split(":", 1)[1]
    f = await _filters(state)
    if value == "__type__":
        await state.set_state(Flow.endseries)
        await show(cq.message.bot, cq.message.chat.id,
                  "⌨️ Надішли <b>2 останні літери</b> серії (напр. <code>КС</code>):",
                  kb_back([("⬅️ Назад", "step:endseries")]))
        await cq.answer()
        return
    f["series_end"] = None if value == "__all__" else value
    await _set_filters(state, f)
    await render_step(cq.message.bot, cq.message.chat.id, state, "price")
    await cq.answer()


@dp.message(Flow.endseries)
async def do_endseries_text(message: Message, state: FSMContext) -> None:
    """Capture a typed ending series (2 letters) → continue the wizard."""
    import re as _re

    raw = message.text or ""
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    letters = _re.sub(r"[^A-Za-zА-Яа-яІЇЄҐіїєґ]", "", raw).upper().translate(_WORD_LAT2CYR)
    if len(letters) < 2:
        # stay in endseries state so the next message retries
        await show(message.bot, message.chat.id,
                   "✋ Треба рівно 2 літери (напр. КС). Надішли ще раз:",
                   kb_back([("⬅️ Назад", "step:endseries")]))
        return
    f = await _filters(state)
    f["series_end"] = letters[-2:]
    await _set_filters(state, f)
    await state.set_state(Flow.search)
    await render_step(message.bot, message.chat.id, state, "price")


@dp.callback_query(F.data.startswith("setpf:"))
async def cb_set_price_fixed(cq: CallbackQuery, state: FSMContext) -> None:
    """Pick a fixed price (or any) → advance to the combo step."""
    value = cq.data.split(":", 1)[1]
    f = await _filters(state)
    if value == "__all__":
        f["price_min"] = f["price_max"] = None
    else:
        price = float(value)
        f["price_min"] = f["price_max"] = price
    await _set_filters(state, f)
    await render_step(cq.message.bot, cq.message.chat.id, state, "combo")
    await cq.answer()


@dp.message(Flow.search)
async def do_combo_text(message: Message, state: FSMContext) -> None:
    """Capture the number/mask on the combo step, else re-render the current step."""
    f = await _filters(state)
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if f.get("step") != "combo":
        await render_step(message.bot, message.chat.id, state, f.get("step", "type"))
        return
    f["query"] = (message.text or "").strip() or None
    f["page"] = 0
    await _set_filters(state, f)
    await _finalize(message.bot, message.chat.id, state)


_PAGE = 15
_GRID = 18  # номери як кнопки: 6 рядків × 3


def _kw(f: dict) -> dict:
    """Build the filter kwargs shared by search_filtered / count_filtered."""
    return dict(
        query=f.get("query"), region=f.get("region"), letters_start=f.get("series"),
        letters_end=f.get("series_end"),
        vehicle_type=f.get("vtype"), price_min=f.get("price_min"), price_max=f.get("price_max"),
        collection=f.get("collection"),
    )


async def _finalize(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """End the wizard: show results (search mode) or save a hunt (hunt mode)."""
    f = await _filters(state)
    if f.get("mode") == "hunt":
        await _save_hunt_from_filters(bot, chat_id, state)
        return
    quota = await db.check_search_quota(chat_id)
    if not quota["allowed"]:
        text = (
            f"⚠️ Ліміт пошуків на сьогодні вичерпано ({quota['used']}/{quota['limit']}).\n\n"
            "Він оновиться завтра. Або отримай безліміт:\n"
            f"• 👥 Запроси {db.PRO_INVITE_THRESHOLD} друзів → 💎 PRO\n"
            "• 💎 PRO — безлімітний пошук"
        )
        await show(bot, chat_id, text, kb_back([("👥 Запросити друзів", "ref"), ("💎 Тариф", "plan")]))
        return
    await db.consume_search(chat_id)
    u = await db.get_user(chat_id)
    await db.log_search(chat_id, u.get("username") if u else None, _summary(f))
    f["page"] = 0
    await _set_filters(state, f)
    await render_results(bot, chat_id, state)


@dp.callback_query(F.data == "s_skip")
async def cb_skip(cq: CallbackQuery, state: FSMContext) -> None:
    """Skip the combo step → finalize (results or save hunt)."""
    f = await _filters(state)
    f["query"] = None
    await _set_filters(state, f)
    await _finalize(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "mk_hunt")
async def cb_make_hunt(cq: CallbackQuery, state: FSMContext) -> None:
    """Create a monitoring from the just-searched filters (offered when search found nothing)."""
    await _save_hunt_from_filters(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


async def render_results(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Render a compact, paginated list of plate NUMBERS only (details on demand)."""
    f = await _filters(state)
    page = f.get("page", 0)
    kw = _kw(f)
    total = await db.count_filtered(**kw)
    rows = await db.search_filtered(limit=_GRID, offset=page * _GRID, **kw)
    if f.get("collection"):
        crumbs = (f"{db.COLLECTIONS.get(f['collection'])} · 🚗 {f.get('vtype') or 'всі'} · "
                  f"🌍 {f.get('region') or 'всі'}")
    else:
        crumbs = _summary(f)
    if not rows:
        b = InlineKeyboardBuilder()
        b.button(text="🔔 Стежити за такими (моніторинг)", callback_data="mk_hunt")
        b.button(text="🔄 Новий пошук", callback_data="search")
        b.button(text="⬅️ Меню", callback_data="menu")
        b.adjust(1)
        await show(
            bot, chat_id,
            f"🔍 Нічого не знайдено\n<i>{crumbs}</i>\n\n"
            "🔔 Можу створити <b>моніторинг</b> на ці параметри — щойно такий номер зʼявиться, "
            "одразу надішлю сповіщення.",
            b.as_markup(),
        )
        return
    pages_total = (total + _GRID - 1) // _GRID
    start = page * _GRID
    text = (f"🔍 <b>Результати</b> ({total:,})".replace(",", " ")
            + f" · стор. {page + 1}/{pages_total}\n<i>{crumbs}</i>\n\nОбери номер 👇")
    b = InlineKeyboardBuilder()
    for r in rows:  # номери як кнопки, 3 в рядок → тап відкриває картку
        b.button(text=r["plate_number"], callback_data=f"pd:{r['plate_number']}")
    has_prev, has_next = page > 0, start + len(rows) < total
    if has_prev:
        b.button(text="◀️ Назад", callback_data="pg:prev")
    if has_next:
        b.button(text="➡️ Далі", callback_data="pg:next")
    b.button(text="🔄 Новий пошук", callback_data="search")
    b.button(text="⬅️ Меню", callback_data="menu")
    layout = [3] * (len(rows) // 3)
    if len(rows) % 3:
        layout.append(len(rows) % 3)
    nav = int(has_prev) + int(has_next)
    if nav:
        layout.append(nav)
    layout += [2]
    b.adjust(*layout)
    await show(bot, chat_id, text, b.as_markup())


@dp.callback_query(F.data == "pg:next")
async def cb_pg_next(cq: CallbackQuery, state: FSMContext) -> None:
    """Next results page."""
    f = await _filters(state)
    f["page"] = f.get("page", 0) + 1
    await _set_filters(state, f)
    await render_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "pg:prev")
async def cb_pg_prev(cq: CallbackQuery, state: FSMContext) -> None:
    """Previous results page."""
    f = await _filters(state)
    f["page"] = max(0, f.get("page", 0) - 1)
    await _set_filters(state, f)
    await render_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "s_results")
async def cb_results(cq: CallbackQuery, state: FSMContext) -> None:
    """Return to the results list from the details picker."""
    await render_results(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "s_details")
async def cb_details(cq: CallbackQuery, state: FSMContext) -> None:
    """Show buttons (one per plate of the current page) to open details."""
    f = await _filters(state)
    page = f.get("page", 0)
    rows = await db.search_filtered(limit=_PAGE, offset=page * _PAGE, **_kw(f))
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=r["plate_number"], callback_data=f"pd:{r['plate_number']}")
    b.button(text="⬅️ До результатів", callback_data="s_results")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id,
              "🔎 <b>Обери номер</b> для деталей:", b.as_markup())
    await cq.answer()


async def render_detail(bot: Bot, chat_id: int, state: FSMContext, plate: str,
                        note: Optional[str] = None) -> None:
    """Render full details of one plate + favorite toggle and a back button."""
    f = await _filters(state)
    back_cb = {"favs": "favs", "hunt": "hd_details", "feed": "f_show",
               "results": "s_results"}.get(f.get("dorigin"), "s_results")
    locs = await db.plate_locations(plate)
    fav = await db.is_favorite(chat_id, plate)
    digits = parse_plate(plate).get("digits")
    fav_total = await db.favorites_combo_count(digits)
    hunt_total = await db.hunts_combo_count(digits)
    lines = []
    if note:
        lines.append(note + "\n")
    lines.append(_plate_card(plate))
    if not locs:
        lines.append("дані відсутні")
    for l in locs:
        price = f"{int(l['price']):,} грн".replace(",", " ") if l.get("price") else "—"
        mark = "✅ доступний" if l["is_available"] else "❌ зник"
        addr = l.get("tsc_address") or "—"
        line = (
            f"\n📍 {l['region']} · {l.get('tsc') or '—'}\n   {addr}\n"
            f"   🚗 {l.get('vehicle_type') or '—'} · 💰 {price} · {mark}"
        )
        if l.get("first_seen_at"):
            line += f"\n   🟢 Виявлено: {_fmt_dt(l['first_seen_at'])}"
        if l.get("removed_at"):
            line += f"\n   🔴 Зник: {_fmt_dt(l['removed_at'])}"
        lines.append(line)
    combo = digits or "—"
    lines.append(
        f"\n📊 <b>Комбінація {combo}</b>:\n"
        f"⭐ в обраному у {fav_total} · 🎯 моніторять {hunt_total}"
    )
    b = InlineKeyboardBuilder()
    if fav:
        b.button(text="💔 Прибрати з обраних", callback_data=f"unfav:{plate}")
    else:
        b.button(text="⭐ Додати до улюблених", callback_data=f"fav:{plate}")
    link = _ref_link(chat_id)
    share_text = f"Дивись який номер: {plate} 🚗 Шукай свій у «Моніторинг Автономерів»:"
    b.button(text="📤 Поділитися", url=f"https://t.me/share/url?url={quote(link)}&text={quote(share_text)}")
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(bot, chat_id, "\n".join(lines), b.as_markup())


@dp.callback_query(F.data.startswith("pd:"))
async def cb_plate_detail(cq: CallbackQuery, state: FSMContext) -> None:
    """Open plate details from the search results."""
    f = await _filters(state)
    f["dorigin"] = "results"
    await _set_filters(state, f)
    await render_detail(cq.message.bot, cq.message.chat.id, state, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("pdf:"))
async def cb_plate_detail_fav(cq: CallbackQuery, state: FSMContext) -> None:
    """Open plate details from the favorites list."""
    f = await _filters(state)
    f["dorigin"] = "favs"
    await _set_filters(state, f)
    await render_detail(cq.message.bot, cq.message.chat.id, state, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(cq: CallbackQuery, state: FSMContext) -> None:
    """Add a plate to favorites; show updated popularity."""
    plate = cq.data.split(":", 1)[1]
    await db.add_favorite(cq.message.chat.id, plate)
    await render_detail(cq.message.bot, cq.message.chat.id, state, plate, note="✅ Додано в обране ⭐")
    await cq.answer("Додано в обране ⭐")


@dp.callback_query(F.data.startswith("unfav:"))
async def cb_unfav(cq: CallbackQuery, state: FSMContext) -> None:
    """Remove a plate from favorites."""
    plate = cq.data.split(":", 1)[1]
    await db.remove_favorite(cq.message.chat.id, plate)
    await render_detail(cq.message.bot, cq.message.chat.id, state, plate, note="💔 Прибрано з обраних.")
    await cq.answer("Прибрано")


@dp.callback_query(F.data == "favs")
async def cb_favs(cq: CallbackQuery, state: FSMContext) -> None:
    """List the user's favorite plates as buttons."""
    favs = await db.list_favorites(cq.message.chat.id)
    if not favs:
        await show(cq.message.bot, cq.message.chat.id,
                   "⭐ <b>Обрані</b>\n\nПоки порожньо. Додай номери з пошуку 🔍",
                   kb_back([("🔍 Пошук", "search")]))
        await cq.answer()
        return
    b = InlineKeyboardBuilder()
    for p in favs:
        b.button(text=p, callback_data=f"pdf:{p}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id,
              f"⭐ <b>Обрані</b> ({len(favs)})\nОбери номер:", b.as_markup())
    await cq.answer()


# ── hunts ─────────────────────────────────────────
@dp.callback_query(F.data == "newhunt")
async def cb_newhunt(cq: CallbackQuery, state: FSMContext) -> None:
    """Start the same wizard in HUNT mode (type → region → series → price → digits)."""
    await state.set_state(Flow.search)
    await _set_filters(state, {"mode": "hunt"})
    await render_step(cq.message.bot, cq.message.chat.id, state, "type")
    await cq.answer()


async def _save_hunt_from_filters(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Build a hunt from the wizard filters, save it, and confirm with stats."""
    from local.plate import to_search_like

    # Enforce the freemium hunt limit (FREE = base + referral bonus; PRO = unlimited).
    used = await db.active_hunt_count(chat_id)
    limit = await db.hunt_limit(chat_id)
    if used >= limit:
        text = (
            f"⚠️ Ліміт моніторингів вичерпано ({used}/{limit}).\n\n"
            "🆓 FREE дозволяє кілька моніторингів. Щоб додати більше:\n"
            "• 👥 Запроси друзів — за кожного +1 слот\n"
            f"• Запроси {db.PRO_INVITE_THRESHOLD} друзів → 💎 PRO (безліміт) на {db.PRO_DAYS_FOR_INVITES} днів"
        )
        await show(bot, chat_id, text, kb_back([("👥 Запросити друзів", "ref"), ("🎯 Мої моніторинги", "myhunts")]))
        return

    f = await _filters(state)
    h: dict = {
        "match_type": "filters",
        "letters_start": f.get("series"),
        "letters_end": f.get("series_end"),
        "region": f.get("region"),
        "vehicle_type": f.get("vtype"),
        "price_min": f.get("price_min"),
        "price_max": f.get("price_max"),
    }
    q = f.get("query")
    if q:
        mode, pattern = to_search_like(q)
        if mode == "digits":
            if "_" in pattern:
                h["digits_mask"] = pattern
            else:
                h["digits_exact"] = pattern
    digits_label = h.get("digits_exact") or (h.get("digits_mask", "").replace("_", "*"))
    le = h.get("letters_end") or ""
    if le:  # word/combination hunt: front + (digits|****) + back
        label = (h.get("letters_start") or "") + (digits_label or "****") + le
    else:
        label = (h.get("letters_start") or "") + (digits_label or "")
    h["pattern"] = label or "будь-який"
    h["name"] = h["pattern"]
    await db.ensure_user(chat_id, None)
    await db.add_hunt(chat_id, h)

    cnt = await db.count_hunt_matches(h)
    sample = await db.list_hunt_matches(h, limit=5)
    new, removed = await db.hunt_changes_24h(h)
    digits = h.get("digits_exact")
    pop = await db.hunts_combo_count(digits) if digits else 0
    lines = ["✅ <b>Моніторинг створено</b>", f"<i>{_summary(f)}</i>", ""]
    if cnt:
        lines.append(f"🔎 Зараз під цей моніторинг: <b>{cnt}</b> номерів")
        if sample:
            lines.append("напр.: " + ", ".join(r["plate_number"] for r in sample))
    else:
        lines.append("🔎 Зараз таких номерів немає — сповіщу, щойно зʼявляться.")
    if digits and pop:
        lines.append(f"🎯 Цю комбінацію моніторять ще <b>{pop}</b> людей")
    lines.append(f"📈 За добу: +{new} нових, −{removed} зниклих")
    await show(bot, chat_id, "\n".join(lines),
               kb_back([("🎯 Мої моніторинги", "myhunts"), ("➕ Ще один", "newhunt")]))


@dp.callback_query(F.data == "myhunts")
async def cb_myhunts(cq: CallbackQuery) -> None:
    """List the user's hunts with toggle/delete buttons."""
    hunts = await db.list_hunts(cq.message.chat.id)
    if not hunts:
        await show(
            cq.message.bot, cq.message.chat.id,
            "🎯 <b>Твої моніторинги</b>\n\nПоки порожньо. Створи перший 👇",
            kb_back([("➕ Новий моніторинг", "newhunt")]),
        )
        await cq.answer()
        return
    lines = ["🎯 <b>Твої моніторинги</b>\n"]
    b = InlineKeyboardBuilder()
    adj = []
    for idx, h in enumerate(hunts, 1):
        status = "✅" if h["is_active"] else "⏸"
        cnt = await db.count_hunt_matches(h)
        digits = h.get("digits_exact")
        pop = await db.hunts_combo_count(digits) if digits else 0
        pop_txt = f" · 🎯 моніторять ще {pop}" if pop else ""
        lines.append(f"<b>{idx}.</b> {status} {_hunt_desc(h)}\n    🔢 збігів зараз: <b>{cnt}</b>{pop_txt}")
        b.button(text=f"🔍 №{idx}", callback_data=f"hview:{h['id']}")
        b.button(text=f"{'⏸' if h['is_active'] else '▶️'} №{idx}", callback_data=f"toggle:{h['id']}")
        b.button(text=f"❌ №{idx}", callback_data=f"del:{h['id']}")
        adj.append(3)
    b.button(text="➕ Новий моніторинг", callback_data="newhunt")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(*adj, 1, 1)
    await show(cq.message.bot, cq.message.chat.id, "\n".join(lines), b.as_markup())
    await cq.answer()


@dp.callback_query(F.data.startswith("hview:"))
async def cb_hview(cq: CallbackQuery, state: FSMContext) -> None:
    """Open a hunt's matches (paginated)."""
    hunt_id = int(cq.data.split(":", 1)[1])
    await state.update_data(hunt_id=hunt_id, hpage=0)
    await render_hunt_view(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


async def render_hunt_view(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Render a paginated list of plate numbers matching the stored hunt."""
    data = await state.get_data()
    hunt_id, page = data.get("hunt_id"), data.get("hpage", 0)
    h = await db.get_hunt(chat_id, hunt_id)
    if not h:
        await show(bot, chat_id, "Моніторинг не знайдено", kb_back([("🎯 Мої моніторинги", "myhunts")]))
        return
    label = h.get("name") or h.get("pattern") or "—"
    total = await db.count_hunt_matches(h)
    rows = await db.list_hunt_matches(h, limit=_PAGE, offset=page * _PAGE)
    new, removed = await db.hunt_changes_24h(h)
    filt = f"🌍 {h.get('region') or 'всі'} · 🚗 {h.get('vehicle_type') or 'будь-який'}"
    if not rows:
        await show(bot, chat_id,
                   f"🎯 <b>{label}</b>\n{filt}\n\nЗараз немає номерів.\n📈 За добу: +{new}, −{removed}",
                   kb_back([("🎯 Мої моніторинги", "myhunts")]))
        return
    pages_total = (total + _PAGE - 1) // _PAGE
    start = page * _PAGE
    listing = "\n".join(f"{start + i + 1}. <b>{r['plate_number']}</b>" for i, r in enumerate(rows))
    text = (f"🎯 <b>{label}</b> ({total}) · стор. {page + 1}/{pages_total}\n{filt}\n"
            f"📈 За добу: +{new}, −{removed}\n\n{listing}")
    b = InlineKeyboardBuilder()
    b.button(text="🔎 Детальніше", callback_data="hd_details")
    has_prev, has_next = page > 0, start + len(rows) < total
    if has_prev:
        b.button(text="◀️ Назад", callback_data="hpg:prev")
    if has_next:
        b.button(text="➡️ Далі", callback_data="hpg:next")
    b.button(text="🎯 Мої моніторинги", callback_data="myhunts")
    b.button(text="⬅️ Меню", callback_data="menu")
    layout = [1]
    nav = int(has_prev) + int(has_next)
    if nav:
        layout.append(nav)
    layout += [1, 1]
    b.adjust(*layout)
    await show(bot, chat_id, text, b.as_markup())


@dp.callback_query(F.data == "hpg:next")
async def cb_hpg_next(cq: CallbackQuery, state: FSMContext) -> None:
    """Next page of hunt matches."""
    data = await state.get_data()
    await state.update_data(hpage=data.get("hpage", 0) + 1)
    await render_hunt_view(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "hpg:prev")
async def cb_hpg_prev(cq: CallbackQuery, state: FSMContext) -> None:
    """Previous page of hunt matches."""
    data = await state.get_data()
    await state.update_data(hpage=max(0, data.get("hpage", 0) - 1))
    await render_hunt_view(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data == "hd_details")
async def cb_hd_details(cq: CallbackQuery, state: FSMContext) -> None:
    """Show buttons (one per matching plate of the current hunt page) for details."""
    data = await state.get_data()
    hunt_id, page = data.get("hunt_id"), data.get("hpage", 0)
    h = await db.get_hunt(cq.message.chat.id, hunt_id)
    if not h:
        await cq.answer("Моніторинг не знайдено", show_alert=True)
        return
    rows = await db.list_hunt_matches(h, limit=_PAGE, offset=page * _PAGE)
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=r["plate_number"], callback_data=f"pdh:{r['plate_number']}")
    b.button(text="⬅️ До моніторингу", callback_data="hd_back")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id, "🔎 <b>Обери номер</b> для деталей:", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "hd_back")
async def cb_hd_back(cq: CallbackQuery, state: FSMContext) -> None:
    """Back from the hunt details picker to the hunt matches list."""
    await render_hunt_view(cq.message.bot, cq.message.chat.id, state)
    await cq.answer()


@dp.callback_query(F.data.startswith("pdh:"))
async def cb_plate_detail_hunt(cq: CallbackQuery, state: FSMContext) -> None:
    """Open plate details from a hunt's matches."""
    f = await _filters(state)
    f["dorigin"] = "hunt"
    await _set_filters(state, f)
    await render_detail(cq.message.bot, cq.message.chat.id, state, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("del:"))
async def cb_del(cq: CallbackQuery) -> None:
    """Delete a hunt and refresh the list."""
    await db.delete_hunt(cq.message.chat.id, int(cq.data.split(":", 1)[1]))
    await cb_myhunts(cq)


@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(cq: CallbackQuery) -> None:
    """Toggle a hunt active/paused and refresh the list."""
    await db.toggle_hunt(cq.message.chat.id, int(cq.data.split(":", 1)[1]))
    await cb_myhunts(cq)


# ── admin panel ───────────────────────────────────
def _is_super(chat_id: int) -> bool:
    """Whether this chat is the configured super-admin."""
    return bool(config.ADMIN_CHAT_ID) and chat_id == config.ADMIN_CHAT_ID


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Open the admin panel (admins only)."""
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if not await db.is_admin(message.chat.id):
        await render_main(message.bot, message.chat.id)
        return
    await render_admin(message.bot, message.chat.id)


async def render_admin(bot: Bot, chat_id: int) -> None:
    """Admin panel — clean top level with three grouped sections."""
    b = InlineKeyboardBuilder()
    b.button(text="📊 Аналітика", callback_data="a_an")
    b.button(text="🅿️ База і парсер", callback_data="a_db")
    b.button(text="⚙️ Керування", callback_data="a_mng")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    role = "супер-адмін" if _is_super(chat_id) else "адмін"
    pending = (await db.get_meta("stage_pending")) == "1"
    note = "\n\n📥 <b>Є оновлення у черзі</b> → База і парсер → Оновити базу" if pending else ""
    await show(bot, chat_id, f"🛠 <b>Адмін-панель</b> ({role}){note}\n\nОбери розділ 👇", b.as_markup())


@dp.callback_query(F.data == "a_an")
async def cb_a_an(cq: CallbackQuery) -> None:
    """Admin section: analytics."""
    if not await db.is_admin(cq.message.chat.id):
        return
    b = InlineKeyboardBuilder()
    b.button(text="📊 Статистика", callback_data="a_stats")
    b.button(text="👥 Користувачі", callback_data="a_users")
    b.button(text="🔎 Активність", callback_data="a_activity")
    b.button(text="🐞 Звіти про помилки", callback_data="a_reports")
    b.button(text="⬅️ Адмінка", callback_data="admin")
    b.adjust(2, 2, 1)
    await show(cq.message.bot, cq.message.chat.id, "📊 <b>Аналітика</b>", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "a_db")
async def cb_a_db(cq: CallbackQuery) -> None:
    """Admin section: database & parser."""
    if not await db.is_admin(cq.message.chat.id):
        return
    pending = (await db.get_meta("stage_pending")) == "1"
    cnt = await db.get_meta("stage_count") or "0"
    b = InlineKeyboardBuilder()
    b.button(text="🅿️ Парсер (регіон/тип, звіт ТСЦ)", callback_data="a_scan")
    b.button(text=(f"🔄 Оновити базу ({cnt})" if pending else "🔄 Оновити базу"), callback_data="a_commit")
    b.button(text="📥 Імпорт CSV", callback_data="a_import")
    b.button(text="⬅️ Адмінка", callback_data="admin")
    b.adjust(1, 2, 1)
    note = f"\n\n📥 У черзі очікує <b>{cnt}</b> номерів." if pending else ""
    await show(cq.message.bot, cq.message.chat.id, f"🅿️ <b>База і парсер</b>{note}", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "a_mng")
async def cb_a_mng(cq: CallbackQuery) -> None:
    """Admin section: management."""
    if not await db.is_admin(cq.message.chat.id):
        return
    b = InlineKeyboardBuilder()
    b.button(text="💎 Надати VIP", callback_data="a_vip")
    b.button(text="🤖 Боти", callback_data="a_bots")
    b.button(text="📣 Розсилка", callback_data="a_bcast")
    b.button(text="📢 Оживити чати", callback_data="a_refresh")
    if _is_super(cq.message.chat.id):
        b.button(text="👮 Адміни", callback_data="a_admins")
    b.button(text="⬅️ Адмінка", callback_data="admin")
    b.adjust(2, 2, 1, 1)
    await show(cq.message.bot, cq.message.chat.id, "⚙️ <b>Керування</b>", b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "admin")
async def cb_admin(cq: CallbackQuery) -> None:
    """Admin panel entry."""
    if not await db.is_admin(cq.message.chat.id):
        await cq.answer("Лише для адмінів", show_alert=True)
        return
    await render_admin(cq.message.bot, cq.message.chat.id)
    await cq.answer()


@dp.callback_query(F.data == "a_refresh")
async def cb_a_refresh(cq: CallbackQuery) -> None:
    """Re-engage all users: send each a fresh 'base updated' menu (bumps the chat to the top)."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await cq.answer("Оживляю чати…")
    n = await push_refresh_all(cq.message.bot)
    await cq.message.bot.send_message(
        cq.message.chat.id, f"📢 Оживлено чатів: <b>{n}</b>",
        reply_markup=kb_back([("🛠 Адмінка", "admin")]),
    )


@dp.callback_query(F.data == "a_stats")
async def cb_a_stats(cq: CallbackQuery) -> None:
    """Admin statistics."""
    if not await db.is_admin(cq.message.chat.id):
        return
    s = await db.admin_stats()
    last = (await db.get_stats()).get("last_scan") or "—"
    text = (
        "📊 <b>Адмін-статистика</b>\n\n"
        f"👥 Усього: <b>{s['users'] + s.get('bots', 0)}</b> "
        f"(люди: {s['users']} · PRO: {s['pro_users']} · боти: {s.get('bots', 0)})\n"
        f"🎯 Моніторингів: <b>{s['hunts']}</b>\n"
        f"⭐ В обраному: <b>{s['favorites']}</b>\n"
        f"🚗 Номерів у базі: <b>{s['plates']:,}</b>".replace(",", " ") + "\n"
        f"🐞 Звітів: <b>{s['reports']}</b>\n"
        f"🕷 Останній скан: {last}"
    )
    await show(cq.message.bot, cq.message.chat.id, text, kb_back([("🛠 Адмінка", "admin")]))
    await cq.answer()


@dp.callback_query(F.data == "a_users")
async def cb_a_users(cq: CallbackQuery) -> None:
    """Show recent users with key info."""
    if not await db.is_admin(cq.message.chat.id):
        return
    users = await db.recent_users(15)
    total = (await db.admin_stats())["users"]
    new1 = await db.new_users_count(1)
    new7 = await db.new_users_count(7)
    text = (f"👥 <b>Користувачі</b>\nВсього: <b>{total}</b> · +{new1} за добу · +{new7} за тиждень\n\n"
            "Обери користувача для деталей 👇")
    b = InlineKeyboardBuilder()
    for u in users:
        plan = "💎" if u.get("plan") == "pro" else "🆓"
        label = ("@" + u["username"]) if u.get("username") else str(u["chat_id"])
        b.button(text=f"{plan} {label}", callback_data=f"auser:{u['chat_id']}")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id, text, b.as_markup())
    await cq.answer()


async def render_user_card(bot: Bot, chat_id: int, uid: int) -> None:
    """Render an admin user-card for ``uid`` into the viewer's (``chat_id``) screen."""
    u = await db.user_overview(uid)
    plan = "💎 PRO" if db.is_pro(u) else "🆓 FREE"
    login = f"@{u['username']}" if u.get("username") else "—"
    phone = u.get("phone") or "—"
    joined = str(u.get("created_at"))[:10] if u.get("created_at") else "—"
    lines = [
        "👤 <b>Користувач</b>",
        f"🆔 <code>{uid}</code>\n👤 {login}\n📱 {phone}",
        f"\n💎 Тариф: {plan} · 📅 з {joined}",
        f"👥 Запрошено друзів: {u.get('invited_count') or 0}"
        + (f" · прийшов від <code>{u['referred_by']}</code>" if u.get("referred_by") else ""),
        f"🎯 Моніторингів: {u.get('hunts', 0)} · ⭐ обране: {u.get('favorites', 0)} · 🔎 пошуків: {u.get('searches', 0)}",
    ]
    if u.get("hunt_list"):
        lines.append("\n<b>Моніторинги:</b>")
        for h in u["hunt_list"]:
            lines.append(f"  • {h.get('name') or '—'} ({h.get('match_type')})")
    if u.get("recent_searches"):
        lines.append("\n<b>Останні пошуки:</b>")
        for sx in u["recent_searches"]:
            lines.append(f"  • {str(sx['created_at'])[11:16]} {sx['summary'][:50]}")
    b = InlineKeyboardBuilder()
    b.button(text="💎 Надати VIP", callback_data=f"vipu:{uid}")
    if _is_super(chat_id) and uid != config.ADMIN_CHAT_ID:
        if await db.is_admin(uid):
            b.button(text="❌ Зняти адміна", callback_data=f"rmadm:{uid}")
        else:
            b.button(text="👮 Зробити адміном", callback_data=f"mkadm:{uid}")
    b.button(text="👥 Користувачі", callback_data="a_users")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(1)
    await show(bot, chat_id, "\n".join(lines), b.as_markup())


@dp.callback_query(F.data.startswith("auser:"))
async def cb_a_user(cq: CallbackQuery) -> None:
    """Detailed profile of one user."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await render_user_card(cq.message.bot, cq.message.chat.id, int(cq.data.split(":", 1)[1]))
    await cq.answer()


@dp.callback_query(F.data.startswith("mkadm:"))
async def cb_mkadm(cq: CallbackQuery) -> None:
    """Super-admin: promote a user to admin (from their card)."""
    if not _is_super(cq.message.chat.id):
        await cq.answer("Лише супер-адмін", show_alert=True)
        return
    uid = int(cq.data.split(":", 1)[1])
    await db.add_admin(uid, cq.message.chat.id)
    try:
        await cq.message.bot.send_message(uid, "👮 Тебе призначено адміном бота. Команда /admin.")
    except Exception:
        pass
    await render_user_card(cq.message.bot, cq.message.chat.id, uid)
    await cq.answer("Призначено адміном 👮")


@dp.callback_query(F.data.startswith("rmadm:"))
async def cb_rmadm(cq: CallbackQuery) -> None:
    """Super-admin: revoke an admin (from their card)."""
    if not _is_super(cq.message.chat.id):
        await cq.answer("Лише супер-адмін", show_alert=True)
        return
    uid = int(cq.data.split(":", 1)[1])
    await db.remove_admin(uid)
    await render_user_card(cq.message.bot, cq.message.chat.id, uid)
    await cq.answer("Знято")


@dp.callback_query(F.data.startswith("vipu:"))
async def cb_vip_user(cq: CallbackQuery, state: FSMContext) -> None:
    """Grant VIP to a specific user (from their card): ask for days."""
    if not await db.is_admin(cq.message.chat.id):
        return
    uid = int(cq.data.split(":", 1)[1])
    await state.set_state(Flow.admin_vip_days)
    await state.update_data(vip_target=uid)
    await show(cq.message.bot, cq.message.chat.id,
              f"💎 Надати VIP користувачу <code>{uid}</code>\n\nВведи кількість днів (число):",
              kb_back([("👤 Назад", f"auser:{uid}")]))
    await cq.answer()


@dp.callback_query(F.data == "a_vip")
async def cb_a_vip(cq: CallbackQuery, state: FSMContext) -> None:
    """Grant VIP via the admin panel: ask for id/username first."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await state.set_state(Flow.admin_vip_user)
    await show(cq.message.bot, cq.message.chat.id,
              "💎 <b>Надати VIP</b>\n\nВведи <b>ID</b> або <b>@нікнейм</b> користувача:",
              kb_back([("🛠 Адмінка", "admin")]))
    await cq.answer()


@dp.message(Flow.admin_vip_user)
async def do_vip_user(message: Message, state: FSMContext) -> None:
    """Resolve the target user, then ask for days."""
    ident = (message.text or "").strip()
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if not await db.is_admin(message.chat.id):
        await state.clear()
        return
    user = await db.find_user_by_identifier(ident)
    if not user:
        await show(message.bot, message.chat.id,
                   f"❌ Користувача «{ident}» не знайдено (він має хоч раз запустити бота).",
                   kb_back([("💎 Спробувати ще", "a_vip"), ("🛠 Адмінка", "admin")]))
        await state.clear()
        return
    await state.set_state(Flow.admin_vip_days)
    await state.update_data(vip_target=user["chat_id"])
    who = f"@{user['username']}" if user.get("username") else str(user["chat_id"])
    await show(message.bot, message.chat.id,
              f"💎 Користувач {who} (<code>{user['chat_id']}</code>)\n\nВведи кількість днів VIP:",
              kb_back([("🛠 Адмінка", "admin")]))


@dp.message(Flow.admin_vip_days)
async def do_vip_days(message: Message, state: FSMContext) -> None:
    """Grant PRO for the given number of days and notify the user."""
    raw = (message.text or "").strip()
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    data = await state.get_data()
    target = data.get("vip_target")
    await state.clear()
    if not await db.is_admin(message.chat.id) or not target:
        return
    if not raw.isdigit() or int(raw) <= 0:
        await show(message.bot, message.chat.id, "❌ Невірна кількість днів.",
                   kb_back([("🛠 Адмінка", "admin")]))
        return
    days = int(raw)
    await db.grant_pro(target, days)
    try:
        await message.bot.send_message(target, f"🎉 Тобі надано 💎 VIP (PRO) на {days} днів! Дякуємо 🚗")
    except Exception:
        pass
    await show(message.bot, message.chat.id,
              f"✅ VIP на {days} днів надано користувачу <code>{target}</code>.",
              kb_back([("👤 Картка", f"auser:{target}"), ("🛠 Адмінка", "admin")]))


@dp.callback_query(F.data == "a_activity")
async def cb_a_activity(cq: CallbackQuery) -> None:
    """Show what users search: recent + top queries."""
    if not await db.is_admin(cq.message.chat.id):
        return
    recent = await db.recent_searches(12)
    top = await db.top_searches(8)
    lines = ["🔎 <b>Активність пошуку</b>\n"]
    if top:
        lines.append("<b>Топ запитів:</b>")
        for t in top:
            lines.append(f"  • {t['summary'][:60]} — {t['c']}")
        lines.append("")
    if recent:
        lines.append("<b>Останні пошуки:</b>")
        for r in recent:
            who = f"@{r['username']}" if r.get("username") else str(r["chat_id"])
            when = str(r["created_at"])[11:16]
            lines.append(f"  • {when} {who}: {r['summary'][:55]}")
    if not recent and not top:
        lines.append("Поки немає даних.")
    await show(cq.message.bot, cq.message.chat.id, "\n".join(lines), kb_back([("🛠 Адмінка", "admin")]))
    await cq.answer()


async def render_bots(bot: Bot, chat_id: int) -> None:
    """Bot-management screen."""
    cnt = await db.bot_count()
    text = (f"🤖 <b>Демо-боти</b>\nЗараз: <b>{cnt}</b>\n\n"
            "Боти додають випадкові красиві номери в обране та моніторинги — "
            "щоб база виглядала активною на етапі тесту.")
    b = InlineKeyboardBuilder()
    for n in (10, 25, 50, 100):
        b.button(text=f"➕ {n}", callback_data=f"bots_add:{n}")
    for n in (10, 25, 50):
        b.button(text=f"➖ {n}", callback_data=f"bots_del:{n}")
    b.button(text="🗑 Видалити всіх", callback_data="bots_delall")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(4, 3, 1, 1)
    await show(bot, chat_id, text, b.as_markup())


@dp.callback_query(F.data == "a_bots")
async def cb_a_bots(cq: CallbackQuery) -> None:
    """Open bot management."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await render_bots(cq.message.bot, cq.message.chat.id)
    await cq.answer()


@dp.callback_query(F.data.startswith("bots_add:"))
async def cb_bots_add(cq: CallbackQuery) -> None:
    """Create N demo bots."""
    if not await db.is_admin(cq.message.chat.id):
        return
    n = int(cq.data.split(":", 1)[1])
    await cq.answer("Генерую…")
    created = await db.create_bots(n)
    await render_bots(cq.message.bot, cq.message.chat.id)
    try:
        await cq.message.bot.send_message(cq.message.chat.id, f"✅ Додано {created} ботів.")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("bots_del:"))
async def cb_bots_del(cq: CallbackQuery) -> None:
    """Delete N random demo bots."""
    if not await db.is_admin(cq.message.chat.id):
        return
    n = int(cq.data.split(":", 1)[1])
    removed = await db.delete_bots(n)
    await render_bots(cq.message.bot, cq.message.chat.id)
    await cq.answer(f"Видалено {removed}")


@dp.callback_query(F.data == "bots_delall")
async def cb_bots_delall(cq: CallbackQuery) -> None:
    """Delete all demo bots."""
    if not await db.is_admin(cq.message.chat.id):
        return
    removed = await db.delete_all_bots()
    await render_bots(cq.message.bot, cq.message.chat.id)
    await cq.answer(f"Видалено всіх ({removed})")


@dp.callback_query(F.data == "a_scan")
async def cb_a_scan(cq: CallbackQuery) -> None:
    """Parser launch menu: all regions or a specific one."""
    if not await db.is_admin(cq.message.chat.id):
        return
    b = InlineKeyboardBuilder()
    b.button(text="🌍 Усі регіони (всі типи)", callback_data="a_scan_all")
    b.button(text="📍 Регіон + тип ТЗ", callback_data="a_scan_region")
    b.button(text="📊 Звіт по ТСЦ", callback_data="a_rep_region")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id,
              "🅿️ <b>Парсер</b>\nЗапит іде в чергу; парсер на Маку виконує саме обраний скоуп "
              "(регіон+тип) і шле звіт сюди. Або переглянь розбивку по ТСЦ.",
              b.as_markup())
    await cq.answer()


async def _queue_scan(bot: Bot, chat_id: int, regions=None, only_scopes=None) -> None:
    """Queue a scrape request for the Mac-side worker (the server has no scraper/Playwright).

    Akamai needs a residential IP, so the server only RECORDS the request in the DB; the
    `local.scan_worker` process on the Mac picks it up, scrapes, writes to the shared Supabase
    DB, reports the result here and clears the flag. Manual-trigger by design.
    """
    import datetime as _dt
    import json
    req = {
        "chat_id": chat_id,
        "regions": list(regions) if regions else None,
        "only_scopes": [list(s) for s in only_scopes] if only_scopes else None,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    await db.set_meta("scan_request", json.dumps(req))
    scope = "повтор невдалих" if only_scopes else ("усі регіони" if not regions else ", ".join(regions))
    await show(bot, chat_id,
              f"🕷 Запит на скан (<b>{scope}</b>) поставлено в чергу.\n"
              "Парсер на Маку (residential) виконає його найближчим часом — звіт прийде сюди.",
              kb_back([("🛠 Адмінка", "admin")]))


@dp.callback_query(F.data == "scan_retry")
async def cb_scan_retry(cq: CallbackQuery) -> None:
    """Queue a re-scan of only the previously failed (region, type) scopes (from DB meta)."""
    if not await db.is_admin(cq.message.chat.id):
        return
    import json
    raw = await db.get_meta("last_fail_scopes")
    fails = json.loads(raw) if raw else []
    if not fails:
        await cq.answer("Немає невдалих", show_alert=True)
        return
    await cq.answer("Ставлю в чергу…")
    await _queue_scan(cq.message.bot, cq.message.chat.id, None, only_scopes=[tuple(x) for x in fails])


@dp.callback_query(F.data == "a_scan_all")
async def cb_a_scan_all(cq: CallbackQuery) -> None:
    """Queue a full scan (executed by the Mac worker)."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await _queue_scan(cq.message.bot, cq.message.chat.id, None)
    await cq.answer("Поставлено в чергу")


async def _region_picker(cq: CallbackQuery, action: str, title: str) -> None:
    """Show regions as buttons; callback `<action>:<regionIndex>` (index into distinct_regions)."""
    regions = await db.distinct_regions()
    b = InlineKeyboardBuilder()
    for i, r in enumerate(regions):
        b.button(text=r, callback_data=f"{action}:{i}")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(2)
    await show(cq.message.bot, cq.message.chat.id, title, b.as_markup())
    await cq.answer()


async def _type_picker(cq: CallbackQuery, action: str, region_idx: int, title: str, all_label: str) -> None:
    """Show vehicle types; callback `<action>:<regionIndex>:<typeIndex>` (-1 = all types)."""
    types = await db.distinct_vehicle_types()
    b = InlineKeyboardBuilder()
    b.button(text=all_label, callback_data=f"{action}:{region_idx}:-1")
    for j, t in enumerate(types):
        b.button(text=t, callback_data=f"{action}:{region_idx}:{j}")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id, title, b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "a_scan_region")
async def cb_a_scan_region(cq: CallbackQuery) -> None:
    """Step 1: pick a region to scan."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await _region_picker(cq, "psr", "📍 Обери <b>регіон</b> для парсингу:")


@dp.callback_query(F.data.startswith("psr:"))
async def cb_psr(cq: CallbackQuery) -> None:
    """Step 2: pick a vehicle type for the chosen region."""
    if not await db.is_admin(cq.message.chat.id):
        return
    i = int(cq.data.split(":", 1)[1])
    regions = await db.distinct_regions()
    rname = regions[i] if 0 <= i < len(regions) else "?"
    await _type_picker(cq, "pst", i, f"📍 {rname}\n🚗 Обери <b>тип ТЗ</b> для парсингу:", "✅ Усі типи")


@dp.callback_query(F.data.startswith("pst:"))
async def cb_pst(cq: CallbackQuery) -> None:
    """Step 3: enqueue a scoped scan (region + type) for the Mac worker."""
    if not await db.is_admin(cq.message.chat.id):
        return
    _, si, sj = cq.data.split(":")
    regions = await db.distinct_regions()
    types = await db.distinct_vehicle_types()
    region = regions[int(si)] if 0 <= int(si) < len(regions) else None
    if region is None:
        await cq.answer("Регіон не знайдено", show_alert=True)
        return
    vtype = types[int(sj)] if int(sj) >= 0 and int(sj) < len(types) else None
    only = [[region, vtype]] if vtype else None
    await _queue_scan(cq.message.bot, cq.message.chat.id, {region}, only_scopes=only)
    await cq.answer("Поставлено в чергу")


@dp.callback_query(F.data == "a_rep_region")
async def cb_a_rep_region(cq: CallbackQuery) -> None:
    """TSC report step 1: pick region."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await _region_picker(cq, "rsr", "📊 Звіт по ТСЦ — обери <b>регіон</b>:")


@dp.callback_query(F.data.startswith("rsr:"))
async def cb_rsr(cq: CallbackQuery) -> None:
    """TSC report step 2: pick type."""
    if not await db.is_admin(cq.message.chat.id):
        return
    i = int(cq.data.split(":", 1)[1])
    regions = await db.distinct_regions()
    rname = regions[i] if 0 <= i < len(regions) else "?"
    await _type_picker(cq, "rst", i, f"📊 {rname}\n🚗 Обери <b>тип ТЗ</b> для звіту:", "✅ Усі типи")


@dp.callback_query(F.data.startswith("rst:"))
async def cb_rst(cq: CallbackQuery) -> None:
    """TSC report step 3: show per-parking breakdown from the DB."""
    if not await db.is_admin(cq.message.chat.id):
        return
    _, si, sj = cq.data.split(":")
    regions = await db.distinct_regions()
    types = await db.distinct_vehicle_types()
    region = regions[int(si)] if 0 <= int(si) < len(regions) else None
    vtype = types[int(sj)] if int(sj) >= 0 and int(sj) < len(types) else None
    rows = await db.tsc_breakdown(region, vtype)
    head = f"📊 <b>Звіт по ТСЦ</b>\n🌍 {region or 'усі'} · 🚗 {vtype or 'усі типи'}\n"
    if not rows:
        await show(cq.message.bot, cq.message.chat.id, head + "\nНемає доступних номерів.",
                   kb_back([("🅿️ Парсер", "a_scan"), ("🛠 Адмінка", "admin")]))
        await cq.answer()
        return
    total = sum(r["cnt"] for r in rows)
    lines = [head, f"Усього доступних: <b>{total}</b> у {len(rows)} ТСЦ\n"]
    for r in rows[:40]:
        pr = ""
        if r.get("pmin") is not None:
            lo, hi = int(r["pmin"]), int(r["pmax"])
            pr = f" · 💰 {lo}" + (f"–{hi}" if hi != lo else "") + " грн"
        addr = f"\n   {r['address']}" if r.get("address") else ""
        lines.append(f"• <b>{r['tsc'] or '—'}</b>: {r['cnt']} шт{pr}{addr}")
    if len(rows) > 40:
        lines.append(f"…ще {len(rows) - 40} ТСЦ")
    await show(cq.message.bot, cq.message.chat.id, "\n".join(lines),
               kb_back([("🅿️ Парсер", "a_scan"), ("🛠 Адмінка", "admin")]))
    await cq.answer()


async def _commit_task(bot: Bot, chat_id: int) -> None:
    """Apply the staged update queue to the DB and report."""
    from local.persist import commit_staging
    try:
        res = await commit_staging()
        await bot.send_message(
            chat_id,
            f"✅ Базу оновлено з черги.\n📊 Оброблено: {res['processed']}\n🆕 Нових: {res['new']}\n"
            f"❌ Зникло: {res.get('removed', 0)}\n📨 Сповіщень: {res['notified']}",
            reply_markup=kb_back([("🛠 Адмінка", "admin")]),
        )
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(chat_id, f"❌ Помилка оновлення: {exc!r}")


@dp.callback_query(F.data == "a_commit")
async def cb_a_commit(cq: CallbackQuery) -> None:
    """Commit the staged update queue (from the extension) to the DB."""
    if not await db.is_admin(cq.message.chat.id):
        return
    pending = await db.get_meta("stage_pending")
    cnt = await db.get_meta("stage_count") or "0"
    if pending != "1":
        await show(cq.message.bot, cq.message.chat.id,
                   "🔄 <b>Оновити базу</b>\n\nНаразі немає нових даних у черзі очікування.",
                   kb_back([("🛠 Адмінка", "admin")]))
        await cq.answer()
        return
    ts = await db.get_meta("stage_ts") or "?"
    await show(cq.message.bot, cq.message.chat.id,
               f"🔄 Оновлюю базу з черги ({cnt} номерів, від {_fmt_dt(ts)})…",
               kb_back([("🛠 Адмінка", "admin")]))
    await cq.answer("Оновлюю…")
    asyncio.create_task(_commit_task(cq.message.bot, cq.message.chat.id))


@dp.callback_query(F.data == "a_import")
async def cb_a_import(cq: CallbackQuery, state: FSMContext) -> None:
    """Prompt the admin to upload a CSV table (alternative DB-update method)."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await state.set_state(Flow.admin_csv)
    await show(
        cq.message.bot, cq.message.chat.id,
        "📥 <b>Імпорт таблиці (CSV)</b>\n\nНадішли CSV-файл (з розширення «Автономера — таблиця»): "
        "стовпці Номер;Ціна;Сервісний центр;Регіон;Тип ТЗ.\n\n"
        "Оновлю базу: додам нові, оновлю наявні, нові потраплять у стрічку та сповіщення. "
        "<i>Зникнення старих при ручному імпорті НЕ враховується.</i>",
        kb_back([("🛠 Адмінка", "admin")]),
    )
    await cq.answer()


async def _process_csv_task(bot: Bot, chat_id: int, text: str) -> None:
    """Parse an uploaded CSV table and apply it to the DB (background)."""
    from local.persist import apply_table, notify_new, parse_table_csv
    try:
        rows = parse_table_csv(text)
        if not rows:
            await bot.send_message(chat_id, "⚠️ У файлі не знайдено рядків. Перевір формат (Номер;Ціна;ТСЦ;Регіон;Тип).")
            return
        await bot.send_message(chat_id, f"⏳ Обробляю {len(rows)} рядків… (може зайняти 1–3 хв)")
        res = await apply_table(rows)
        notified = await notify_new(res["new_ids"])
        await bot.send_message(
            chat_id,
            f"✅ Імпорт завершено.\n📊 Оброблено: {res['processed']}\n🆕 Нових: {len(res['new_ids'])}\n"
            f"📨 Сповіщень: {notified}",
            reply_markup=kb_back([("🛠 Адмінка", "admin")]),
        )
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(chat_id, f"❌ Помилка імпорту: {exc!r}")


@dp.message(Flow.admin_csv, F.document)
async def do_csv_import(message: Message, state: FSMContext) -> None:
    """Receive the CSV document from an admin and import it."""
    if not await db.is_admin(message.chat.id):
        return
    await state.clear()
    doc = message.document
    try:
        f = await message.bot.get_file(doc.file_id)
        buf = await message.bot.download_file(f.file_path)
        raw = buf.read() if hasattr(buf, "read") else buf
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"❌ Не вдалося завантажити файл: {exc!r}")
        return
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    asyncio.create_task(_process_csv_task(message.bot, message.chat.id, text))
    await show(message.bot, message.chat.id, "📥 Файл отримано, обробляю у фоні…",
               kb_back([("🛠 Адмінка", "admin")]))


@dp.callback_query(F.data == "a_reports")
async def cb_a_reports(cq: CallbackQuery) -> None:
    """Show recent error reports."""
    if not await db.is_admin(cq.message.chat.id):
        return
    reports = await db.recent_reports(12)
    if not reports:
        text = "🐞 <b>Звіти</b>\n\nПоки немає."
    else:
        lines = ["🐞 <b>Останні звіти</b>\n"]
        for r in reports:
            who = f"@{r['username']}" if r.get("username") else str(r["chat_id"])
            when = _fmt_dt(r["created_at"])
            lines.append(f"• {when} · {who}\n  {r['text'][:200]}")
        text = "\n".join(lines)
    await show(cq.message.bot, cq.message.chat.id, text, kb_back([("🛠 Адмінка", "admin")]))
    await cq.answer()


@dp.callback_query(F.data == "a_bcast")
async def cb_a_bcast(cq: CallbackQuery, state: FSMContext) -> None:
    """Prompt for a broadcast message."""
    if not await db.is_admin(cq.message.chat.id):
        return
    await state.set_state(Flow.admin_broadcast)
    await show(cq.message.bot, cq.message.chat.id,
              "📣 <b>Розсилка</b>\n\nНадішли текст — він піде ВСІМ користувачам.",
              kb_back([("🛠 Адмінка", "admin")]))
    await cq.answer()


@dp.message(Flow.admin_broadcast)
async def do_bcast(message: Message, state: FSMContext) -> None:
    """Send a broadcast to all users."""
    text = (message.text or "").strip()
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await state.clear()
    if not await db.is_admin(message.chat.id) or not text:
        await render_admin(message.bot, message.chat.id)
        return
    ids = await db.all_user_ids()
    sent = 0
    for uid in ids:
        try:
            await message.bot.send_message(uid, f"📣 {text}")
            sent += 1
        except Exception:
            pass
    await show(message.bot, message.chat.id,
              f"✅ Розіслано {sent}/{len(ids)} користувачам.", kb_back([("🛠 Адмінка", "admin")]))


@dp.callback_query(F.data == "a_admins")
async def cb_a_admins(cq: CallbackQuery) -> None:
    """Manage admins (super-admin only)."""
    if not _is_super(cq.message.chat.id):
        await cq.answer("Лише супер-адмін", show_alert=True)
        return
    admins = await db.list_admins()
    lines = ["👮 <b>Адміни</b>\n", f"👑 Супер-адмін: <code>{config.ADMIN_CHAT_ID}</code>"]
    b = InlineKeyboardBuilder()
    for a in admins:
        lines.append(f"• <code>{a['chat_id']}</code>")
        b.button(text=f"❌ {a['chat_id']}", callback_data=f"a_deladm:{a['chat_id']}")
    b.button(text="➕ Додати адміна", callback_data="a_addadm")
    b.button(text="🛠 Адмінка", callback_data="admin")
    b.adjust(1)
    await show(cq.message.bot, cq.message.chat.id, "\n".join(lines), b.as_markup())
    await cq.answer()


@dp.callback_query(F.data == "a_addadm")
async def cb_a_addadm(cq: CallbackQuery, state: FSMContext) -> None:
    """Prompt for a new admin's chat id."""
    if not _is_super(cq.message.chat.id):
        return
    await state.set_state(Flow.admin_addadmin)
    await show(cq.message.bot, cq.message.chat.id,
              "➕ Надішли <b>chat_id</b> нового адміна (число).\n"
              "Користувач може дізнатись свій id у бота @userinfobot.",
              kb_back([("👮 Адміни", "a_admins")]))
    await cq.answer()


@dp.message(Flow.admin_addadmin)
async def do_addadm(message: Message, state: FSMContext) -> None:
    """Register a new admin by chat id."""
    raw = (message.text or "").strip()
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    await state.clear()
    if not _is_super(message.chat.id):
        return
    if raw.lstrip("-").isdigit():
        await db.add_admin(int(raw), message.chat.id)
        try:
            await message.bot.send_message(int(raw), "👮 Тебе призначено адміном бота. Команда /admin.")
        except Exception:
            pass
        await show(message.bot, message.chat.id, f"✅ Адміна <code>{raw}</code> додано.",
                   kb_back([("👮 Адміни", "a_admins")]))
    else:
        await show(message.bot, message.chat.id, "❌ Невірний id.", kb_back([("👮 Адміни", "a_admins")]))


@dp.callback_query(F.data.startswith("a_deladm:"))
async def cb_a_deladm(cq: CallbackQuery) -> None:
    """Remove an admin."""
    if not _is_super(cq.message.chat.id):
        return
    await db.remove_admin(int(cq.data.split(":", 1)[1]))
    await cb_a_admins(cq)


@dp.message()
async def fallback(message: Message) -> None:
    """Stray text outside a flow: a full plate → AutoCheck; else show the menu."""
    plate = _full_plate(message.text or "")
    await _safe_delete(message.bot, message.chat.id, message.message_id)
    if plate:
        await show(message.bot, message.chat.id, "🔎 Перевіряю номер у реєстрі МВС…", kb_back())
        res = await _autocheck_query(plate)
        await show(message.bot, message.chat.id, _fmt_ac_summary(res, plate), _ac_menu_kb(res, plate))
        return
    await render_main(message.bot, message.chat.id)


async def _auto_commit_loop(bot: Bot) -> None:
    """Auto-commit the staged queue if it has waited longer than STAGE_AUTOCOMMIT_HOURS."""
    import datetime as dt
    while True:
        await asyncio.sleep(1800)  # check every 30 min
        try:
            if (await db.get_meta("stage_pending")) != "1":
                continue
            ts = await db.get_meta("stage_ts")
            if not ts:
                continue
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ts)
            if age.total_seconds() >= config.STAGE_AUTOCOMMIT_HOURS * 3600:
                from local.persist import commit_staging
                res = await commit_staging()
                if config.ADMIN_CHAT_ID:
                    await bot.send_message(
                        config.ADMIN_CHAT_ID,
                        f"♻️ <b>Авто-оновлення бази</b> (минуло {config.STAGE_AUTOCOMMIT_HOURS} год без ручного):\n"
                        f"оброблено {res['processed']}, нових {res['new']}, зникло {res.get('removed', 0)}, "
                        f"сповіщень {res['notified']}.",
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"[autocommit] {exc!r}")


async def _periodic_refresh(bot: Bot) -> None:
    """Every config.REFRESH_HOURS, bump all chats with a fresh 'base updated' menu."""
    while True:
        await asyncio.sleep(config.REFRESH_HOURS * 3600)
        try:
            n = await push_refresh_all(bot)
            print(f"[refresh] periodic: bumped {n} chats")
        except Exception as exc:  # noqa: BLE001
            print(f"[refresh] periodic failed: {exc!r}")


async def main() -> None:
    """Initialise the DB and start long polling."""
    if not config.BOT_TOKEN:
        raise SystemExit("LOCAL_BOT_TOKEN is not set (create a product bot via @BotFather)")
    global BOT_USERNAME
    await db.init_db()
    await db.warm_cache()  # pre-compute hot aggregates so the first menu open is instant
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        me = await bot.get_me()
        if me.username:
            BOT_USERNAME = me.username
    except Exception:
        pass
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск / оновити екран"),
        BotCommand(command="clear", description="Очистити чат"),
        BotCommand(command="report", description="Повідомити про помилку"),
    ])
    try:
        await bot.set_my_description(
            "🇺🇦 Моніторинг Автономерів. Пошук автомобільних номерів ГСЦ МВС "
            "по всій Україні: за серією, регіоном, цифрами та ціною. Налаштуй моніторинг "
            "і отримай сповіщення, щойно зʼявиться твій номер."
        )
        await bot.set_my_short_description(
            "Моніторинг автономерів України 🇺🇦 — пошук авто-номерів ГСЦ МВС і сповіщення."
        )
    except Exception:
        pass
    if config.REFRESH_HOURS > 0:
        asyncio.create_task(_periodic_refresh(bot))
    asyncio.create_task(_auto_commit_loop(bot))
    print(f"Bot @{BOT_USERNAME} started (Моніторинг Автономерів, long polling). Ctrl+C to stop.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
