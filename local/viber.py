"""Viber bot adapter — second channel over the same DB/logic as the Telegram bot.

Webhook-based (Viber pushes events to /viber/webhook on the server). Viber has no message
editing, so each screen is a fresh message with a reply keyboard (ActionBody = command).
Reuses local.db for all data; a tiny in-memory per-user state drives the simple wizards.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from local import config, db

API = "https://chatapi.viber.com/pp"
SENDER = {"name": "Моніторинг Автономерів"}
_PAGE = 10
# Per-user transient state (api process memory): {viber_id: {"await": "search"|"monitor", ...}}.
_state: Dict[str, Dict[str, Any]] = {}


def valid_signature(body: bytes, signature: str) -> bool:
    """Verify Viber's X-Viber-Content-Signature (HMAC-SHA256 of the raw body with the token)."""
    if not config.VIBER_TOKEN or not signature:
        return False
    mac = hmac.new(config.VIBER_TOKEN.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature)


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path, data=data,
        headers={"X-Viber-Auth-Token": config.VIBER_TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _kb(buttons: List[Tuple[str, str, int]]) -> dict:
    """Build a Viber reply keyboard. buttons = [(text, action_body, columns 1-6), …]."""
    return {
        "Type": "keyboard", "DefaultHeight": False, "BgColor": "#f4f7f4",
        "Buttons": [
            {"Columns": c, "Rows": 1, "ActionType": "reply", "ActionBody": ab,
             "Text": f"<font color='#0b3d0b'>{t}</font>", "TextSize": "regular",
             "BgColor": "#d8efd8"} for (t, ab, c) in buttons
        ],
    }


async def send(receiver: str, text: str, buttons: Optional[List[Tuple[str, str, int]]] = None) -> None:
    """Send a text message (optionally with a reply keyboard) to a Viber user."""
    payload: Dict[str, Any] = {"receiver": receiver, "type": "text", "text": text, "sender": SENDER}
    if buttons:
        payload["keyboard"] = _kb(buttons)
    try:
        await asyncio.to_thread(_post, "/send_message", payload)
    except Exception as exc:  # noqa: BLE001
        print(f"[viber] send failed: {exc!r}")


def set_webhook() -> dict:
    """Register the webhook with Viber (call once after deploy / token change)."""
    return _post("/set_webhook", {
        "url": config.VIBER_WEBHOOK_URL,
        "event_types": ["message", "subscribed", "conversation_started", "unsubscribed"],
        "send_name": True, "send_photo": False,
    })


def _menu_kb() -> List[Tuple[str, str, int]]:
    return [
        ("🔔 Стежити за номером", "monitor", 6),
        ("🎯 Мої моніторинги", "mylist", 6),
        ("🔍 Пошук номера", "search", 3),
        ("📰 Нові / зниклі", "feed", 3),
        ("📊 Статистика", "stats", 3),
        ("ℹ️ Довідка", "help", 3),
    ]


async def _send_menu(uid: str, banner: str = "") -> None:
    s = await db.get_stats()
    total = s.get("total") or 0
    text = (
        (banner + "\n\n" if banner else "")
        + "🇺🇦 Моніторинг Автономерів\n"
        "Постав номер на стеження — і дізнайся першим, щойно він зʼявиться.\n\n"
        f"📦 У базі: {total:,} номерів".replace(",", " ") + "\n\n"
        "👇 Обери дію"
    )
    await send(uid, text, _menu_kb())


def _fmt_row(r: Dict[str, Any]) -> str:
    price = r.get("price")
    pr = f"{int(price):,} грн".replace(",", " ") if price else "—"
    addr = r.get("tsc_address") or r.get("tsc") or "—"
    return f"🚗 {r['plate_number']} · {r.get('region') or ''} · {pr}\n   📍 {addr}"


async def _do_search(uid: str, query: str) -> None:
    rows = await db.search_filtered(query=query, limit=_PAGE)
    if not rows:
        await send(uid, f"🔍 За запитом «{query}» нічого не знайдено.\n"
                        "Можеш поставити моніторинг — натисни «Стежити за номером».",
                   [("🔔 Стежити за номером", "monitor", 6), ("⬅️ Меню", "menu", 6)])
        return
    lines = [f"🔍 Результати за «{query}»:\n"] + [_fmt_row(r) for r in rows]
    await send(uid, "\n".join(lines), [("🔍 Новий пошук", "search", 3), ("⬅️ Меню", "menu", 3)])


async def _do_mylist(uid: str) -> None:
    # Viber user id is its own namespace; we store hunts under the viber id with a 'v:' prefix chat.
    hunts = await db.list_hunts(_chat(uid))
    if not hunts:
        await send(uid, "🎯 У тебе ще немає моніторингів.\nНатисни «Стежити за номером», щоб створити.",
                   [("🔔 Стежити за номером", "monitor", 6), ("⬅️ Меню", "menu", 6)])
        return
    lines = ["🎯 Твої моніторинги:\n"]
    for i, h in enumerate(hunts, 1):
        lines.append(f"{i}. {h.get('name') or h.get('pattern') or '—'} · {h.get('region') or 'всі'}")
    await send(uid, "\n".join(lines), [("🔔 Ще один", "monitor", 3), ("⬅️ Меню", "menu", 3)])


async def _do_feed(uid: str) -> None:
    new = await db.feed("new", "day", limit=_PAGE)
    rem = await db.feed("removed", "day", limit=5)
    lines = ["📰 За сьогодні:\n", f"🆕 Нові ({len(new)}):"]
    lines += [f"  • {r['plate_number']} · {r.get('region') or ''}" for r in new[:8]] or ["  —"]
    lines += [f"\n❌ Зниклі ({len(rem)}):"]
    lines += [f"  • {r['plate_number']} · {r.get('region') or ''}" for r in rem[:5]] or ["  —"]
    await send(uid, "\n".join(lines), [("⬅️ Меню", "menu", 6)])


async def _do_stats(uid: str) -> None:
    s = await db.get_stats()
    by_region = s.get("by_region", [])[:6]
    lines = [f"📊 Статистика\n📦 Всього: {s.get('total', 0):,}".replace(",", " "),
             f"✅ Доступних: {s.get('available', 0):,}".replace(",", " "), "\nТоп регіонів:"]
    lines += [f"  • {r['region']}: {r['c']}" for r in by_region]
    await send(uid, "\n".join(lines), [("⬅️ Меню", "menu", 6)])


def _chat(uid: str) -> int:
    """Map a Viber user id to a stable negative integer chat id (separate namespace from Telegram)."""
    return -(2_000_000_000 + (int(hashlib.sha1(uid.encode()).hexdigest()[:8], 16)))


async def _create_monitor(uid: str, text: str) -> None:
    """Create a simple monitoring from a free-text plate/mask (e.g. 'АА1234' or 'АА' or '1234')."""
    from local.plate import to_search_like
    q = text.strip().upper()
    fields: Dict[str, Any] = {"match_type": "filters", "name": q, "pattern": q}
    mode, pattern = to_search_like(q)
    if mode == "digits":
        if "_" in pattern:
            fields["digits_mask"] = pattern
        else:
            fields["digits_exact"] = pattern
    else:
        # letters present → treat as a starts-with series filter
        import re
        letters = re.sub(r"[^A-ZА-ЯІЇЄҐ]", "", q.translate(str.maketrans(
            {"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І", "K": "К",
             "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})))
        if letters:
            fields["letters_start"] = letters[:2]
    await db.ensure_user(_chat(uid), f"viber_{uid[:8]}")
    await db.add_hunt(_chat(uid), fields)
    cnt = await db.count_hunt_matches(fields)
    msg = (f"✅ Моніторинг «{q}» створено.\n"
           + (f"Зараз під нього: {cnt} номерів." if cnt else "Зараз таких немає — сповіщу, щойно зʼявляться."))
    await send(uid, msg, [("🎯 Мої моніторинги", "mylist", 6), ("⬅️ Меню", "menu", 6)])


async def dispatch(uid: str, text: str) -> None:
    """Route a user message: a command button (ActionBody) or free-text for the current step."""
    cmd = (text or "").strip()
    st = _state.get(uid, {})
    if st.get("await") == "search" and cmd not in ("menu", "search", "monitor", "mylist", "feed", "stats", "help"):
        _state.pop(uid, None)
        await _do_search(uid, cmd)
        return
    if st.get("await") == "monitor" and cmd not in ("menu", "search", "monitor", "mylist", "feed", "stats", "help"):
        _state.pop(uid, None)
        await _create_monitor(uid, cmd)
        return
    if cmd in ("menu", "/start", "старт", "меню"):
        _state.pop(uid, None)
        await _send_menu(uid)
    elif cmd == "search":
        _state[uid] = {"await": "search"}
        await send(uid, "🔍 Надішли номер або частину (напр. <АА1234>, <1234>, <АА>):", [("⬅️ Меню", "menu", 6)])
    elif cmd == "monitor":
        _state[uid] = {"await": "monitor"}
        await send(uid, "🔔 Надішли номер/серію/цифри для стеження (напр. <АА1234>, <АА>, <7777>):",
                   [("⬅️ Меню", "menu", 6)])
    elif cmd == "mylist":
        await _do_mylist(uid)
    elif cmd == "feed":
        await _do_feed(uid)
    elif cmd == "stats":
        await _do_stats(uid)
    elif cmd == "help":
        await send(uid, "ℹ️ Я шукаю та стежу за автономерами ГСЦ МВС.\n"
                        "• «Пошук» — знайти доступні номери.\n"
                        "• «Стежити за номером» — сповіщу, щойно зʼявиться потрібний.\n"
                        "• «Нові/зниклі» — зміни за сьогодні.", [("⬅️ Меню", "menu", 6)])
    else:
        # Unknown free text with no active step → treat as a search.
        await _do_search(uid, cmd)


async def handle(event: Dict[str, Any]) -> None:
    """Process one Viber webhook event."""
    etype = event.get("event")
    if etype in ("conversation_started", "subscribed"):
        user = event.get("user") or event.get("sender") or {}
        uid = user.get("id")
        if uid:
            await _send_menu(uid, banner="👋 Вітаю!")
    elif etype == "message":
        uid = (event.get("sender") or {}).get("id")
        msg = event.get("message") or {}
        if uid:
            await dispatch(uid, msg.get("text", ""))
    # 'webhook' (validation ping) and others: no action.
