"""Shared scan-persistence logic — used by the local scan AND the server /ingest endpoint.

Takes scraped rows + the set of successfully-scanned (region, type) scopes, applies them to
the DB (upsert, scope-safe mark_removed, reconcile, feed events) and notifies matching hunts.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Tuple

from local import config, db
from local.matcher import matches


def _fmt_plate_msg(plate: Dict[str, Any], hunt_name: str) -> str:
    """Format a 'found' notification message."""
    price = plate.get("price")
    price_str = f"{int(price):,} грн".replace(",", " ") if price else "—"
    return (
        "🎯 ЗНАЙДЕНО НОМЕР!\n\n"
        f"🚗 {plate['plate_number']}\n"
        f"📍 {plate['region']} › {plate.get('tsc') or '—'}\n"
        f"🏷 Тип: {plate.get('vehicle_type') or 'будь-який'}\n"
        f"💰 Ціна: {price_str}\n"
        f"🔍 Твоя охота: {hunt_name or '—'}"
    )


async def apply_scan(rows: List[Dict[str, Any]], ok_scopes) -> Dict[str, Any]:
    """Persist scraped rows; mark removed only within successfully-scanned scopes.

    Returns a summary dict including the list of new plate ids (for notifications).
    """
    await db.init_db()
    seeded = (await db.get_meta("seeded")) == "1"
    new_ids: List[int] = []
    seen_by_scope: Dict[Tuple[str, Any], List[int]] = defaultdict(list)
    removed = 0

    async with db.acquire() as conn:
        seen_tsc: set = set()
        for r in rows:
            pid, is_new = await db.upsert_plate(
                conn, r["plate_number"], r["region"], r.get("tsc"), r.get("vehicle_type"), r.get("price")
            )
            seen_by_scope[(r["region"], r.get("vehicle_type"))].append(pid)
            if r.get("tsc") and r["tsc"] not in seen_tsc:
                await db.upsert_tsc_region(conn, r["tsc"], r["region"])
                seen_tsc.add(r["tsc"])
            if is_new:
                new_ids.append(pid)
                if seeded:
                    await db.log_feed_event(conn, r["plate_number"], r["region"], r.get("vehicle_type"), "new")
        for (region, vtype) in ok_scopes:
            ids = seen_by_scope.get((region, vtype), [])
            removed_rows = await db.mark_removed(conn, region, vtype, ids)
            removed += len(removed_rows)
            for rr in removed_rows:
                await db.log_feed_event(conn, rr["plate_number"], rr["region"], rr.get("vehicle_type"), "removed")
        await db.reconcile_removed(conn)

    if not seeded:
        await db.set_meta("seeded", "1")
    import datetime as dt
    await db.set_meta("last_scan", dt.datetime.now(dt.timezone.utc).isoformat())
    return {"scraped": len(rows), "new_ids": new_ids, "removed": removed}


async def apply_table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Manual-import variant: upsert rows + track NEW plates, but NEVER mark removed.

    Used for CSV tables uploaded from the admin panel (e.g. the browser-extension export).
    Disappearance is intentionally ignored for the manual method (partial coverage), so we
    only refresh/insert and emit 'new' feed events + notifications.
    """
    await db.init_db()
    seeded = (await db.get_meta("seeded")) == "1"
    new_ids: List[int] = []
    async with db.acquire() as conn:
        seen_tsc: set = set()
        for r in rows:
            pid, is_new = await db.upsert_plate(
                conn, r["plate_number"], r["region"], r.get("tsc"), r.get("vehicle_type"), r.get("price")
            )
            if r.get("tsc") and r["tsc"] not in seen_tsc:
                await db.upsert_tsc_region(conn, r["tsc"], r["region"])
                seen_tsc.add(r["tsc"])
            if is_new:
                new_ids.append(pid)
                if seeded:
                    await db.log_feed_event(conn, r["plate_number"], r["region"], r.get("vehicle_type"), "new")
    if not seeded:
        await db.set_meta("seeded", "1")
    import datetime as dt
    await db.set_meta("last_scan", dt.datetime.now(dt.timezone.utc).isoformat())
    return {"processed": len(rows), "new_ids": new_ids}


async def commit_staging() -> Dict[str, Any]:
    """Apply the staged rows (from /stage) to the DB, then clear the queue."""
    import json

    rows: List[Dict[str, Any]] = []
    try:
        with open(config.STAGE_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        rows = []
    res = await apply_table(rows) if rows else {"processed": 0, "new_ids": []}
    notified = await notify_new(res["new_ids"]) if rows else 0
    try:
        open(config.STAGE_PATH, "w", encoding="utf-8").close()
    except OSError:
        pass
    await db.set_meta("stage_pending", "0")
    await db.set_meta("stage_count", "0")
    return {"processed": res["processed"], "new": len(res["new_ids"]), "notified": notified}


def parse_table_csv(text: str) -> List[Dict[str, Any]]:
    """Parse the extension's CSV (Номер;Ціна;Сервісний центр;Регіон;Тип ТЗ) into row dicts."""
    import csv
    import io

    text = text.lstrip("﻿")
    # Auto-detect delimiter (the extension uses ';', but be tolerant of ',').
    delim = ";" if text[:200].count(";") >= text[:200].count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows: List[Dict[str, Any]] = []
    for i, rec in enumerate(reader):
        if not rec or len(rec) < 5:
            continue
        plate = (rec[0] or "").strip()
        if not plate or plate.lower() in ("номер", "plate"):  # skip header
            continue
        price = None
        try:
            price = float(str(rec[1]).replace(",", ".").strip()) if rec[1].strip() else None
        except ValueError:
            price = None
        rows.append({
            "plate_number": plate, "price": price,
            "tsc": (rec[2] or "").strip() or None,
            "region": (rec[3] or "").strip() or "—",
            "vehicle_type": (rec[4] or "").strip() or None,
        })
    return rows


async def notify_new(new_ids: List[int]) -> int:
    """Match new plates against active hunts and send Telegram notifications."""
    if not config.BOT_TOKEN or not new_ids:
        return 0
    from aiogram import Bot

    bot = Bot(token=config.BOT_TOKEN)
    sent = 0
    try:
        async with db.acquire() as conn:
            hunts = await db.active_hunts(conn)
            if not hunts:
                return 0
            for pid in new_ids:
                plate = await db.get_plate(conn, pid)
                if not plate:
                    continue
                for hunt in hunts:
                    if not matches(plate, hunt):
                        continue
                    if await db.already_notified(conn, hunt["id"], pid):
                        continue
                    try:
                        msg = await bot.send_message(hunt["chat_id"], _fmt_plate_msg(plate, hunt.get("name")))
                        await db.add_notif_message(hunt["chat_id"], msg.message_id)
                        await db.record_notified(conn, hunt["id"], pid)
                        sent += 1
                    except Exception as exc:  # noqa: BLE001
                        print(f"[notify] send failed: {exc!r}")
    finally:
        await bot.session.close()
    return sent
