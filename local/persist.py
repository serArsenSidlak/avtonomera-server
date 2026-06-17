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
    import datetime as dt

    await db.init_db()
    seeded = (await db.get_meta("seeded")) == "1"
    new_ids: List[int] = []
    seen_by_scope: Dict[Tuple[str, Any], List[int]] = defaultdict(list)
    removed = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    async with db.acquire() as conn:
        # Batched upsert (few round trips instead of one per row).
        upserted = await db.bulk_upsert_plates(conn, rows, now)
        new_events: List[tuple] = []
        for u in upserted:
            seen_by_scope[(u["region"], u["vehicle_type"])].append(u["id"])
            if u["inserted"]:
                new_ids.append(u["id"])
                if seeded:
                    new_events.append((u["plate_number"], u["region"], u["vehicle_type"], "new"))
        # TSC → region (once per distinct TSC).
        seen_tsc: set = set()
        for r in rows:
            if r.get("tsc") and r["tsc"] not in seen_tsc:
                await db.upsert_tsc_region(conn, r["tsc"], r["region"])
                seen_tsc.add(r["tsc"])
        # Scope-safe removals.
        removed_events: List[tuple] = []
        for (region, vtype) in ok_scopes:
            ids = seen_by_scope.get((region, vtype), [])
            removed_rows = await db.mark_removed(conn, region, vtype, ids)
            removed += len(removed_rows)
            for rr in removed_rows:
                removed_events.append((rr["plate_number"], rr["region"], rr.get("vehicle_type"), "removed"))
        await db.log_feed_events_bulk(conn, new_events + removed_events)
        await db.reconcile_removed(conn)

    if not seeded:
        await db.set_meta("seeded", "1")
    await db.set_meta("last_scan", now)
    db.invalidate_cache()
    return {"scraped": len(rows), "new_ids": new_ids, "removed": removed}


async def apply_table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Manual-import variant: upsert rows + track NEW plates, but NEVER mark removed.

    Used for CSV tables uploaded from the admin panel (e.g. the browser-extension export).
    Disappearance is intentionally ignored for the manual method (partial coverage), so we
    only refresh/insert and emit 'new' feed events + notifications.
    """
    import datetime as dt

    await db.init_db()
    seeded = (await db.get_meta("seeded")) == "1"
    new_ids: List[int] = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    async with db.acquire() as conn:
        upserted = await db.bulk_upsert_plates(conn, rows, now)
        new_events: List[tuple] = []
        seen_tsc: set = set()
        for u in upserted:
            if u["inserted"]:
                new_ids.append(u["id"])
                if seeded:
                    new_events.append((u["plate_number"], u["region"], u["vehicle_type"], "new"))
        for r in rows:
            if r.get("tsc") and r["tsc"] not in seen_tsc:
                await db.upsert_tsc_region(conn, r["tsc"], r["region"])
                seen_tsc.add(r["tsc"])
        await db.log_feed_events_bulk(conn, new_events)
    if not seeded:
        await db.set_meta("seeded", "1")
    await db.set_meta("last_scan", now)
    db.invalidate_cache()
    return {"processed": len(rows), "new_ids": new_ids}


async def commit_staging() -> Dict[str, Any]:
    """Apply the staged FULL snapshot to the DB (scope-safe removals included), then clear it.

    The extension collects every region×type comprehensively, so the queue is a complete
    snapshot → we use apply_scan with the collected ok_scopes, which also marks genuinely
    disappeared plates as removed (within successfully-collected scopes only).
    """
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
    raw_scopes = await db.get_meta("stage_scopes")
    ok_scopes = set()
    if raw_scopes:
        try:
            ok_scopes = {(s[0], s[1]) for s in json.loads(raw_scopes)}
        except ValueError:
            ok_scopes = set()
    if not ok_scopes:
        # Fallback: derive scopes from the rows themselves (misses zero-result scopes).
        ok_scopes = {(r.get("region"), r.get("vehicle_type")) for r in rows}
    res = await apply_scan(rows, ok_scopes) if rows else {"scraped": 0, "new_ids": [], "removed": 0}
    notified = await notify_new(res["new_ids"]) if rows else 0
    try:
        open(config.STAGE_PATH, "w", encoding="utf-8").close()
    except OSError:
        pass
    await db.set_meta("stage_pending", "0")
    await db.set_meta("stage_count", "0")
    await db.set_meta("stage_scopes", "")
    return {"processed": res.get("scraped", 0), "new": len(res["new_ids"]),
            "removed": res.get("removed", 0), "notified": notified}


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
