"""SQLite storage for the local MVP (aiosqlite, Python 3.9).

Mirrors the production Postgres concepts in a single file DB so the whole app runs with no
server. The schema is intentionally simple; migration to Postgres happens later.
"""
from __future__ import annotations

import datetime as dt
import random
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from local import config
from local.plate import parse_plate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_number    TEXT NOT NULL,
    letters_start   TEXT,
    letters_end     TEXT,
    digits          TEXT,
    digits_int      INTEGER,
    region          TEXT NOT NULL,
    tsc             TEXT,
    vehicle_type    TEXT,
    price           REAL,
    is_available    INTEGER DEFAULT 1,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    removed_at      TEXT,
    UNIQUE(plate_number, tsc, vehicle_type)
);
CREATE INDEX IF NOT EXISTS idx_plates_number ON plates(plate_number);
CREATE INDEX IF NOT EXISTS idx_plates_region ON plates(region);
CREATE INDEX IF NOT EXISTS idx_plates_digits ON plates(digits);
CREATE INDEX IF NOT EXISTS idx_plates_ls ON plates(letters_start);
CREATE INDEX IF NOT EXISTS idx_plates_le ON plates(letters_end);

CREATE TABLE IF NOT EXISTS users (
    chat_id     INTEGER PRIMARY KEY,
    username    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hunts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    name            TEXT,
    match_type      TEXT NOT NULL,
    pattern         TEXT,
    letters_start   TEXT,
    letters_end     TEXT,
    digits_exact    TEXT,
    digits_contains TEXT,
    digits_mask     TEXT,
    region          TEXT,
    vehicle_type    TEXT,
    price_min       REAL,
    price_max       REAL,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hunts_chat ON hunts(chat_id);

CREATE TABLE IF NOT EXISTS notified (
    hunt_id     INTEGER NOT NULL,
    plate_id    INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (hunt_id, plate_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    chat_id      INTEGER NOT NULL,
    plate_number TEXT NOT NULL,
    digits       TEXT,
    region       TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (chat_id, plate_number)
);
CREATE INDEX IF NOT EXISTS idx_fav_plate ON favorites(plate_number);

CREATE TABLE IF NOT EXISTS tsc (
    code        TEXT PRIMARY KEY,
    region      TEXT,
    name        TEXT,
    address     TEXT,
    city        TEXT,
    lat         REAL,
    lon         REAL
);

CREATE TABLE IF NOT EXISTS admins (
    chat_id    INTEGER PRIMARY KEY,
    added_by   INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    username   TEXT,
    text       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notif_messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifmsg_chat ON notif_messages(chat_id);

CREATE TABLE IF NOT EXISTS search_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    username   TEXT,
    summary    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_searchlog_created ON search_log(created_at);

CREATE TABLE IF NOT EXISTS feed_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plate_number TEXT NOT NULL,
    region       TEXT,
    vehicle_type TEXT,
    event        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feed_evt ON feed_events(event, created_at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS app_link (
    code TEXT PRIMARY KEY, chat_id INTEGER, token TEXT, status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applink_token ON app_link(token);
"""


def _now() -> str:
    """UTC ISO timestamp string."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def init_db() -> None:
    """Create tables if they do not exist."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=10000;")
        await db.executescript(_SCHEMA)
        # Migrate older favorites tables that predate digits/region columns.
        for col in ("digits TEXT", "region TEXT"):
            try:
                await db.execute(f"ALTER TABLE favorites ADD COLUMN {col}")
            except Exception:
                pass
        try:
            await db.execute("ALTER TABLE hunts ADD COLUMN digits_mask TEXT")
        except Exception:
            pass
        for col in ("referred_by INTEGER", "invited_count INTEGER DEFAULT 0",
                    "bonus_hunts INTEGER DEFAULT 0", "plan TEXT DEFAULT 'free'", "plan_until TEXT",
                    "shared INTEGER DEFAULT 0", "searches_today INTEGER DEFAULT 0", "searches_date TEXT",
                    "phone TEXT", "is_bot INTEGER DEFAULT 0"):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fav_digits ON favorites(digits)")
        await db.commit()


@asynccontextmanager
async def acquire():
    """Yield a transactional connection (commits on clean exit) for multi-step scan writes."""
    async with aiosqlite.connect(config.DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=10000;")
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def upsert_plate(
    db: aiosqlite.Connection,
    plate_number: str,
    region: str,
    tsc: str,
    vehicle_type: Optional[str],
    price: Optional[float],
) -> Tuple[int, bool]:
    """Insert or refresh a plate. Returns (plate_id, is_new).

    ``is_new`` is True for a fresh insert or a reappeared (previously removed) plate.
    """
    parts = parse_plate(plate_number)
    now = _now()
    cur = await db.execute(
        "SELECT id, is_available FROM plates WHERE plate_number=? AND tsc=? AND "
        "(vehicle_type IS ? OR vehicle_type=?)",
        (parts["plate_number"], tsc, vehicle_type, vehicle_type),
    )
    row = await cur.fetchone()
    if row is None:
        cur = await db.execute(
            "INSERT INTO plates (plate_number, letters_start, letters_end, digits, digits_int, "
            "region, tsc, vehicle_type, price, is_available, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?,?)",
            (
                parts["plate_number"], parts["letters_start"], parts["letters_end"],
                parts["digits"], parts["digits_int"], region, tsc, vehicle_type, price, now, now,
            ),
        )
        return int(cur.lastrowid), True
    plate_id, was_available = int(row[0]), int(row[1])
    await db.execute(
        "UPDATE plates SET is_available=1, removed_at=NULL, last_seen_at=?, price=? WHERE id=?",
        (now, price, plate_id),
    )
    return plate_id, not bool(was_available)


async def bulk_upsert_plates(db: aiosqlite.Connection, rows: List[Dict[str, Any]], now: str = None) -> List[Dict[str, Any]]:
    """Loop upsert for SQLite (local file is fast); same return shape as the Postgres batch."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        pid, is_new = await upsert_plate(
            db, r["plate_number"], r["region"], r.get("tsc"), r.get("vehicle_type"), r.get("price")
        )
        out.append({"id": pid, "plate_number": parse_plate(r["plate_number"])["plate_number"],
                    "region": r["region"], "vehicle_type": r.get("vehicle_type"), "inserted": is_new})
    return out


async def mark_removed(
    db: aiosqlite.Connection, region: str, vehicle_type: Optional[str], seen_ids: List[int]
) -> List[Dict[str, Any]]:
    """Mark available plates of a (region, vehicle_type) scope not in seen_ids as removed.

    Scoped to a SUCCESSFULLY-scanned (region, type) so a failed scan never looks like a mass
    disappearance. Returns the removed rows so the caller can log feed events.
    """
    now = _now()
    placeholders = ",".join("?" for _ in seen_ids) or "0"
    sel_params: List[Any] = [region, vehicle_type, vehicle_type] + seen_ids
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        f"SELECT id, plate_number, region, vehicle_type FROM plates "
        f"WHERE region=? AND (vehicle_type IS ? OR vehicle_type=?) "
        f"AND is_available=1 AND id NOT IN ({placeholders})",
        sel_params,
    )
    removed = [dict(r) for r in await cur.fetchall()]
    if removed:
        ids = [r["id"] for r in removed]
        ph2 = ",".join("?" for _ in ids)
        await db.execute(
            f"UPDATE plates SET is_available=0, removed_at=? WHERE id IN ({ph2})",
            [now] + ids,
        )
    return removed


# ── feed events (genuine per-scan changes; excludes the initial bulk seed) ──
async def reconcile_removed(db: aiosqlite.Connection) -> int:
    """Cancel 'removed' feed events for plates that are currently available again.

    Implements: if a plate reappears among scraped results, its earlier disappearance was a
    glitch (or a genuine return) → it must NOT be counted as removed.
    """
    cur = await db.execute(
        "DELETE FROM feed_events WHERE event='removed' AND plate_number IN "
        "(SELECT plate_number FROM plates WHERE is_available=1)"
    )
    return cur.rowcount or 0


async def log_feed_event(
    db: aiosqlite.Connection, plate_number: str, region: str,
    vehicle_type: Optional[str], event: str,
) -> None:
    """Record a 'new' or 'removed' feed event (called per scan)."""
    await db.execute(
        "INSERT INTO feed_events (plate_number, region, vehicle_type, event, created_at) "
        "VALUES (?,?,?,?,?)",
        (plate_number, region, vehicle_type, event, _now()),
    )


async def log_feed_events_bulk(db: aiosqlite.Connection, events: List[tuple]) -> None:
    """Insert many feed events at once. events = [(plate, region, vehicle_type, event), …]."""
    if not events:
        return
    now = _now()
    await db.executemany(
        "INSERT INTO feed_events (plate_number, region, vehicle_type, event, created_at) VALUES (?,?,?,?,?)",
        [(e[0], e[1], e[2], e[3], now) for e in events],
    )


async def search_plates(
    query: str, limit: int = 20, only_available: bool = True
) -> List[Dict[str, Any]]:
    """Search plates by normalised substring of the plate number."""
    from local.plate import normalize_plate

    q = "%" + normalize_plate(query) + "%"
    sql = (
        "SELECT p.plate_number, p.region, p.tsc, p.vehicle_type, p.price, p.is_available, "
        "t.address AS tsc_address FROM plates p LEFT JOIN tsc t ON p.tsc = t.code "
        "WHERE p.plate_number LIKE ?"
    )
    if only_available:
        sql += " AND p.is_available=1"
    sql += " ORDER BY p.price IS NULL, p.price LIMIT ?"
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, (q, limit))
        return [dict(r) for r in await cur.fetchall()]


async def distinct_regions() -> List[str]:
    """Regions that have plates, sorted."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT region FROM plates ORDER BY region")
        return [r[0] for r in await cur.fetchall()]


async def distinct_vehicle_types() -> List[str]:
    """Distinct vehicle types present, sorted."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT DISTINCT vehicle_type FROM plates WHERE vehicle_type IS NOT NULL ORDER BY vehicle_type"
        )
        return [r[0] for r in await cur.fetchall()]


async def distinct_prices(
    region: Optional[str] = None,
    tsc: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    limit: int = 60,
) -> List[float]:
    """Distinct available prices for the given filters, ascending.

    Prices on the portal are discrete and few per vehicle type, so they work better as
    fixed choices than as ranges.
    """
    where = ["price IS NOT NULL", "is_available = 1"]
    params: List[Any] = []
    if region:
        where.append("region = ?")
        params.append(region)
    if tsc:
        where.append("tsc = ?")
        params.append(tsc)
    if vehicle_type:
        where.append("vehicle_type = ?")
        params.append(vehicle_type)
    params.append(limit)
    sql = (
        "SELECT DISTINCT price FROM plates WHERE " + " AND ".join(where)
        + " ORDER BY price LIMIT ?"
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        return [float(r[0]) for r in await cur.fetchall()]


# Curated "beautiful number" collections (predicates over the 4-digit part).
COLLECTIONS: Dict[str, str] = {
    "same": "🔢 Однакові (7777)",
    "mirror": "🪞 Дзеркальні (1221)",
    "pairs": "👯 Пари (1122)",
    "abab": "🔁 Чергування (1212)",
    "round": "🔟 Круглі (4400)",
    "low": "1️⃣ Низькі (0001–0099)",
}


def _collection_clause(kind: str, a: str = "") -> str:
    """Return a safe SQL predicate (no params) for a curated collection."""
    d, di = f"{a}digits", f"{a}digits_int"
    return {
        "same": f"length({d})=4 AND substr({d},1,1)=substr({d},2,1) AND "
                f"substr({d},2,1)=substr({d},3,1) AND substr({d},3,1)=substr({d},4,1)",
        "mirror": f"length({d})=4 AND substr({d},1,1)=substr({d},4,1) AND substr({d},2,1)=substr({d},3,1)",
        "pairs": f"length({d})=4 AND substr({d},1,1)=substr({d},2,1) AND substr({d},3,1)=substr({d},4,1)",
        "abab": f"length({d})=4 AND substr({d},1,1)=substr({d},3,1) AND substr({d},2,1)=substr({d},4,1)",
        "round": f"length({d})=4 AND {d} LIKE '%00'",
        "low": f"{di} BETWEEN 1 AND 99",
    }.get(kind, "1=1")


def invalidate_cache() -> None:
    """No-op for the SQLite backend (local file DB is already fast)."""
    return None


async def warm_cache() -> None:
    """No-op for the SQLite backend."""
    return None


async def collection_counts() -> List[Dict[str, Any]]:
    """Curated collections with live counts."""
    out: List[Dict[str, Any]] = []
    for k, v in COLLECTIONS.items():
        out.append({"key": k, "label": v, "count": await count_filtered(collection=k)})
    return out


async def popular_combos(limit: int = 10) -> List[Dict[str, Any]]:
    """Most-favorited digit combinations (social proof / discovery)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT digits, COUNT(DISTINCT chat_id) c FROM favorites "
            "WHERE digits IS NOT NULL GROUP BY digits ORDER BY c DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def distinct_series(
    region: Optional[str] = None, vehicle_type: Optional[str] = None, limit: int = 60
) -> List[str]:
    """Distinct plate series (letters_start, e.g. АВ, КВ) for the given filters, sorted."""
    where = ["letters_start IS NOT NULL", "is_available = 1"]
    params: List[Any] = []
    if region:
        where.append("region = ?")
        params.append(region)
    if vehicle_type:
        where.append("vehicle_type = ?")
        params.append(vehicle_type)
    params.append(limit)
    sql = (
        "SELECT letters_start, COUNT(*) c FROM plates WHERE " + " AND ".join(where)
        + " GROUP BY letters_start ORDER BY c DESC LIMIT ?"
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        return [r[0] for r in await cur.fetchall()]


async def tsc_for_region(region: str) -> List[Dict[str, Any]]:
    """TSC codes (with city) for a region, sorted by code."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT code, city FROM tsc WHERE region=? ORDER BY code", (region,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def search_filtered(
    query: Optional[str] = None,
    region: Optional[str] = None,
    tsc: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    letters_start: Optional[str] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    collection: Optional[str] = None,
    limit: int = 15,
    offset: int = 0,
    only_available: bool = True,
) -> List[Dict[str, Any]]:
    """Search plates with optional text query + region/tsc/series/type/price/collection filters."""
    from local.plate import to_search_like

    where = []
    params: List[Any] = []
    if collection:
        where.append(_collection_clause(collection, "p."))
    if query:
        mode, pattern = to_search_like(query)
        where.append("p.digits LIKE ?" if mode == "digits" else "p.plate_number LIKE ?")
        params.append(pattern)
    if region:
        where.append("p.region = ?")
        params.append(region)
    if tsc:
        where.append("p.tsc = ?")
        params.append(tsc)
    if letters_start:
        where.append("p.letters_start = ?")
        params.append(letters_start)
    if vehicle_type:
        where.append("p.vehicle_type = ?")
        params.append(vehicle_type)
    if price_min is not None:
        where.append("p.price >= ?")
        params.append(price_min)
    if price_max is not None:
        where.append("p.price <= ?")
        params.append(price_max)
    if only_available:
        where.append("p.is_available = 1")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    params.append(offset)
    sql = (
        "SELECT p.plate_number, p.region, p.tsc, p.vehicle_type, p.price, p.is_available, "
        "t.address AS tsc_address FROM plates p LEFT JOIN tsc t ON p.tsc = t.code"
        + where_sql + " ORDER BY p.price IS NULL, p.price, p.plate_number LIMIT ? OFFSET ?"
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def count_filtered(
    query: Optional[str] = None,
    region: Optional[str] = None,
    tsc: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    letters_start: Optional[str] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    collection: Optional[str] = None,
    only_available: bool = True,
) -> int:
    """Count plates matching the same filters as search_filtered."""
    from local.plate import to_search_like

    where = []
    params: List[Any] = []
    if collection:
        where.append(_collection_clause(collection, ""))
    if query:
        mode, pattern = to_search_like(query)
        where.append("digits LIKE ?" if mode == "digits" else "plate_number LIKE ?")
        params.append(pattern)
    if region:
        where.append("region = ?")
        params.append(region)
    if tsc:
        where.append("tsc = ?")
        params.append(tsc)
    if letters_start:
        where.append("letters_start = ?")
        params.append(letters_start)
    if vehicle_type:
        where.append("vehicle_type = ?")
        params.append(vehicle_type)
    if price_min is not None:
        where.append("price >= ?")
        params.append(price_min)
    if price_max is not None:
        where.append("price <= ?")
        params.append(price_max)
    if only_available:
        where.append("is_available = 1")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM plates" + where_sql, params)
        return int((await cur.fetchone())[0])


_PERIOD_MODIFIER = {"day": "-1 day", "week": "-7 days", "month": "-30 days"}


def _feed_where(kind: str, period: str, region: Optional[str], vehicle_type: Optional[str]) -> tuple:
    """Build (where, params) over the feed_events table (genuine per-scan changes)."""
    mod = _PERIOD_MODIFIER.get(period, "-1 day")
    where = ["event = ?", "created_at >= datetime('now', ?)"]
    params: List[Any] = ["removed" if kind == "removed" else "new", mod]
    if region:
        where.append("region = ?")
        params.append(region)
    if vehicle_type:
        where.append("vehicle_type = ?")
        params.append(vehicle_type)
    return where, params


async def feed(
    kind: str, period: str, region: Optional[str] = None, vehicle_type: Optional[str] = None,
    limit: int = 15, offset: int = 0,
) -> List[Dict[str, Any]]:
    """Recently added (kind='new') or removed (kind='removed') plate events within a period."""
    where, params = _feed_where(kind, period, region, vehicle_type)
    sql = (
        "SELECT plate_number, region, vehicle_type, created_at AS event_at FROM feed_events "
        "WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params + [limit, offset])
        return [dict(r) for r in await cur.fetchall()]


async def feed_count(kind: str, period: str, region: Optional[str] = None, vehicle_type: Optional[str] = None) -> int:
    """Count feed events for the given filters."""
    where, params = _feed_where(kind, period, region, vehicle_type)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM feed_events WHERE " + " AND ".join(where), params)
        return int((await cur.fetchone())[0])


async def tsc_breakdown(region: Optional[str] = None, vehicle_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Per-TSC breakdown of AVAILABLE plates (count, price range, address) for a region+type."""
    where = ["p.is_available = 1"]
    params: List[Any] = []
    if region:
        where.append("p.region = ?")
        params.append(region)
    if vehicle_type:
        where.append("p.vehicle_type = ?")
        params.append(vehicle_type)
    sql = (
        "SELECT p.tsc, COUNT(*) cnt, MIN(p.price) pmin, MAX(p.price) pmax, t.address "
        "FROM plates p LEFT JOIN tsc t ON p.tsc = t.code WHERE " + " AND ".join(where)
        + " GROUP BY p.tsc, t.address ORDER BY cnt DESC"
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def plate_locations(plate_number: str) -> List[Dict[str, Any]]:
    """All rows (TSC/type/price/address) for a given plate number."""
    from local.plate import normalize_plate

    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT p.plate_number, p.region, p.tsc, p.vehicle_type, p.price, p.is_available, "
            "p.first_seen_at, p.last_seen_at, p.removed_at, t.address AS tsc_address "
            "FROM plates p LEFT JOIN tsc t ON p.tsc = t.code "
            "WHERE p.plate_number = ? ORDER BY p.price",
            (normalize_plate(plate_number),),
        )
        return [dict(r) for r in await cur.fetchall()]


# ── favorites ─────────────────────────────────────
async def add_favorite(chat_id: int, plate_number: str) -> int:
    """Add a plate to favorites (storing its digits+region); returns total holders of the plate."""
    from local.plate import parse_plate

    digits = parse_plate(plate_number).get("digits")
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT region FROM plates WHERE plate_number=? LIMIT 1", (plate_number,)
        )
        row = await cur.fetchone()
        region = row[0] if row else None
        await db.execute(
            "INSERT OR IGNORE INTO favorites (chat_id, plate_number, digits, region, created_at) "
            "VALUES (?,?,?,?,?)",
            (chat_id, plate_number, digits, region, _now()),
        )
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM favorites WHERE plate_number=?", (plate_number,))
        return int((await cur.fetchone())[0])


async def favorites_combo_count(digits: Optional[str], region: Optional[str] = None) -> int:
    """How many users favorited ANY plate with this digit combination (optionally per region)."""
    if not digits:
        return 0
    sql = "SELECT COUNT(DISTINCT chat_id) FROM favorites WHERE digits=?"
    params: List[Any] = [digits]
    if region:
        sql += " AND region=?"
        params.append(region)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        return int((await cur.fetchone())[0])


async def hunts_combo_count(digits: Optional[str], region: Optional[str] = None) -> int:
    """How many users hunt this exact digit combination (region match or all-regions hunt)."""
    if not digits:
        return 0
    sql = "SELECT COUNT(DISTINCT chat_id) FROM hunts WHERE is_active=1 AND digits_exact=?"
    params: List[Any] = [digits]
    if region:
        sql += " AND (region=? OR region IS NULL)"
        params.append(region)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        return int((await cur.fetchone())[0])


async def remove_favorite(chat_id: int, plate_number: str) -> None:
    """Remove a plate from a user's favorites."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "DELETE FROM favorites WHERE chat_id=? AND plate_number=?", (chat_id, plate_number)
        )
        await db.commit()


async def is_favorite(chat_id: int, plate_number: str) -> bool:
    """Whether the plate is in the user's favorites."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM favorites WHERE chat_id=? AND plate_number=?", (chat_id, plate_number)
        )
        return await cur.fetchone() is not None


async def favorites_count(plate_number: str) -> int:
    """How many users have this plate in favorites."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM favorites WHERE plate_number=?", (plate_number,))
        return int((await cur.fetchone())[0])


async def list_favorites(chat_id: int) -> List[str]:
    """A user's favorite plate numbers, newest first."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT plate_number FROM favorites WHERE chat_id=? ORDER BY created_at DESC", (chat_id,)
        )
        return [r[0] for r in await cur.fetchall()]


async def get_stats() -> Dict[str, Any]:
    """Return meaningful counters for the stats screen (no misleading single average)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN is_available=1 THEN 1 ELSE 0 END) available, "
            "MIN(CASE WHEN is_available=1 THEN price END) price_min, "
            "MAX(CASE WHEN is_available=1 THEN price END) price_max "
            "FROM plates"
        )
        row = dict(await cur.fetchone())
        cur = await db.execute(
            "SELECT region, COUNT(*) c FROM plates WHERE is_available=1 GROUP BY region ORDER BY c DESC LIMIT 8"
        )
        row["by_region"] = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT vehicle_type t, COUNT(*) c FROM plates WHERE is_available=1 AND vehicle_type IS NOT NULL "
            "GROUP BY vehicle_type ORDER BY c DESC"
        )
        row["by_type"] = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute("SELECT value FROM meta WHERE key='last_scan'")
        m = await cur.fetchone()
        row["last_scan"] = m[0] if m else None
        return row


async def upsert_tsc_region(db: aiosqlite.Connection, code: str, region: str) -> None:
    """Record the region for a TSC code (address filled later by the directory builder)."""
    await db.execute(
        "INSERT INTO tsc (code, region) VALUES (?,?) "
        "ON CONFLICT(code) DO UPDATE SET region=excluded.region",
        (code, region),
    )


async def set_tsc_address(
    code: str, address: Optional[str], city: Optional[str],
    lat: Optional[float] = None, lon: Optional[float] = None, name: Optional[str] = None,
) -> None:
    """Fill address/city/coords for a TSC code from the directory builder."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO tsc (code, address, city, lat, lon, name) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET address=COALESCE(excluded.address, tsc.address), "
            "city=COALESCE(excluded.city, tsc.city), lat=COALESCE(excluded.lat, tsc.lat), "
            "lon=COALESCE(excluded.lon, tsc.lon), name=COALESCE(excluded.name, tsc.name)",
            (code, address, city, lat, lon, name),
        )
        await db.commit()


async def tsc_address(code: str) -> Optional[str]:
    """Return a TSC's address string if known."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT address FROM tsc WHERE code=?", (code,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


# ── admins / reports / broadcast ──────────────────
async def is_admin(chat_id: int) -> bool:
    """True for the super-admin (config) or any registered admin."""
    if config.ADMIN_CHAT_ID and chat_id == config.ADMIN_CHAT_ID:
        return True
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE chat_id=?", (chat_id,))
        return await cur.fetchone() is not None


async def add_admin(chat_id: int, added_by: int) -> None:
    """Register a new admin."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (chat_id, added_by, created_at) VALUES (?,?,?)",
            (chat_id, added_by, _now()),
        )
        await db.commit()


async def remove_admin(chat_id: int) -> None:
    """Remove an admin (super-admin cannot be removed — not stored here)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE chat_id=?", (chat_id,))
        await db.commit()


async def list_admins() -> List[Dict[str, Any]]:
    """List registered (non-super) admins."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM admins ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]


async def add_report(chat_id: int, username: Optional[str], text: str) -> int:
    """Store an error report from a user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO error_reports (chat_id, username, text, created_at) VALUES (?,?,?,?)",
            (chat_id, username, text, _now()),
        )
        await db.commit()
        return int(cur.lastrowid)


async def recent_reports(limit: int = 15) -> List[Dict[str, Any]]:
    """Most recent error reports."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM error_reports ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def all_user_ids() -> List[int]:
    """All real user chat ids (for broadcast; bots excluded)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM users WHERE COALESCE(is_bot,0)=0")
        return [int(r[0]) for r in await cur.fetchall()]


async def admin_stats() -> Dict[str, Any]:
    """Aggregate counts for the admin panel."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        async def one(sql: str) -> int:
            cur = await db.execute(sql)
            return int((await cur.fetchone())[0])
        return {
            "users": await one("SELECT COUNT(*) FROM users WHERE COALESCE(is_bot,0)=0"),
            "bots": await one("SELECT COUNT(*) FROM users WHERE is_bot=1"),
            "pro_users": await one("SELECT COUNT(*) FROM users WHERE plan='pro' AND COALESCE(is_bot,0)=0"),
            "hunts": await one("SELECT COUNT(*) FROM hunts"),
            "favorites": await one("SELECT COUNT(*) FROM favorites"),
            "plates": await one("SELECT COUNT(*) FROM plates"),
            "reports": await one("SELECT COUNT(*) FROM error_reports"),
        }


async def add_notif_message(chat_id: int, message_id: int) -> None:
    """Remember a notification message id so /start can preserve it during a wipe."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO notif_messages (chat_id, message_id, created_at) VALUES (?,?,?)",
            (chat_id, message_id, _now()),
        )
        await db.commit()


async def notif_message_ids(chat_id: int) -> List[int]:
    """Notification message ids for a chat (to keep them when clearing the interface)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT message_id FROM notif_messages WHERE chat_id=?", (chat_id,))
        return [int(r[0]) for r in await cur.fetchall()]


# ── demo bots (seed activity during testing) ──────
async def bot_count() -> int:
    """Number of demo-bot users."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE is_bot=1")
        return int((await cur.fetchone())[0])


async def _beautiful_pool(db: aiosqlite.Connection, limit: int = 25000) -> List[Dict[str, Any]]:
    """Large, diverse sample of available 'attractive' plates for bots to like/monitor.

    Covers same (7777), mirror (1221), pairs (4400), abab (1212), round (3200), low (0001–0099),
    plus extra appealing patterns (round thousands, ends-00, repeated last three).
    """
    kinds = ["same", "mirror", "pairs", "abab", "round", "low"]
    clause = " OR ".join(f"({_collection_clause(k)})" for k in kinds)
    # extra: thousands (X000), and any ending in 00 already covered by round.
    clause += " OR (length(digits)=4 AND digits LIKE '_000')"
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        f"SELECT plate_number, digits, region, vehicle_type FROM plates "
        f"WHERE is_available=1 AND digits IS NOT NULL AND ({clause}) "
        f"ORDER BY RANDOM() LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def create_bots(n: int, fav_range=(20, 80), hunt_range=(15, 60)) -> int:
    """Create ``n`` demo-bot users, each with random beautiful favorites + monitorings."""
    now = _now()
    async with aiosqlite.connect(config.DB_PATH) as db:
        pool = await _beautiful_pool(db)
        if not pool:
            return 0
        digit_pool = list({p["digits"] for p in pool if p["digits"]})
        cur = await db.execute("SELECT MIN(chat_id) FROM users WHERE is_bot=1")
        min_id = (await cur.fetchone())[0]
        next_id = (min_id - 1) if min_id is not None else -1_000_000_001
        created = 0
        for _ in range(n):
            bid = next_id
            next_id -= 1
            await db.execute(
                "INSERT OR IGNORE INTO users (chat_id, username, created_at, is_bot, shared) "
                "VALUES (?,?,?,1,1)",
                (bid, f"bot_{abs(bid) % 1000000}", now),
            )
            favs = random.sample(pool, min(random.randint(*fav_range), len(pool)))
            await db.executemany(
                "INSERT OR IGNORE INTO favorites (chat_id, plate_number, digits, region, created_at) "
                "VALUES (?,?,?,?,?)",
                [(bid, f["plate_number"], f["digits"], f["region"], now) for f in favs],
            )
            hcombos = random.sample(digit_pool, min(random.randint(*hunt_range), len(digit_pool)))
            await db.executemany(
                "INSERT INTO hunts (chat_id, name, match_type, pattern, digits_exact, created_at) "
                "VALUES (?,?, 'digits', ?, ?, ?)",
                [(bid, d, d, d, now) for d in hcombos],
            )
            created += 1
        await db.commit()
        return created


async def delete_bots(n: int) -> int:
    """Delete ``n`` random demo bots and their data."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM users WHERE is_bot=1 ORDER BY RANDOM() LIMIT ?", (n,))
        ids = [int(r[0]) for r in await cur.fetchall()]
        if not ids:
            return 0
        ph = ",".join("?" for _ in ids)
        for tbl in ("favorites", "hunts", "search_log", "users"):
            await db.execute(f"DELETE FROM {tbl} WHERE chat_id IN ({ph})", ids)
        await db.commit()
        return len(ids)


async def delete_all_bots() -> int:
    """Delete every demo bot and its data."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE is_bot=1")
        cnt = int((await cur.fetchone())[0])
        await db.execute("DELETE FROM favorites WHERE chat_id IN (SELECT chat_id FROM users WHERE is_bot=1)")
        await db.execute("DELETE FROM hunts WHERE chat_id IN (SELECT chat_id FROM users WHERE is_bot=1)")
        await db.execute("DELETE FROM search_log WHERE chat_id IN (SELECT chat_id FROM users WHERE is_bot=1)")
        await db.execute("DELETE FROM users WHERE is_bot=1")
        await db.commit()
        return cnt


# ── app account linking (Telegram ↔ app) ──
async def link_create(code: str, token: str) -> None:
    """Start an app↔Telegram link (pending until the bot binds a chat_id)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO app_link (code, token, status, created_at) VALUES (?,?, 'pending', ?)",
            (code, token, _now()),
        )
        await db.commit()


async def link_bind(code: str, chat_id: int) -> bool:
    """Bind a pending link code to a Telegram chat_id. Returns True if bound."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "UPDATE app_link SET chat_id=?, status='linked' WHERE code=? AND status='pending'",
            (chat_id, code),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def link_status(code: str) -> Optional[Dict[str, Any]]:
    """Status of a link code: {status, chat_id, token}."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT status, chat_id, token FROM app_link WHERE code=?", (code,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def token_chat(token: str) -> Optional[int]:
    """Resolve an app token to its linked chat_id (None if not linked)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT chat_id FROM app_link WHERE token=? AND status='linked'", (token,))
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None


async def create_anon_account(token: str, chat_id: int) -> None:
    """Create an anonymous app account (token already 'linked' to a synthetic chat_id)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO app_link (code, chat_id, token, status, created_at) "
            "VALUES (?,?,?, 'linked', ?)", (token, chat_id, token, _now()),
        )
        await db.commit()


async def merge_account(from_chat: int, to_chat: int) -> None:
    """Move favorites + monitorings from one account into another (used when linking)."""
    if from_chat == to_chat:
        return
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO favorites (chat_id, plate_number, digits, region, created_at) "
            "SELECT ?, plate_number, digits, region, created_at FROM favorites WHERE chat_id=?",
            (to_chat, from_chat),
        )
        await db.execute("DELETE FROM favorites WHERE chat_id=?", (from_chat,))
        await db.execute("UPDATE hunts SET chat_id=? WHERE chat_id=?", (to_chat, from_chat))
        await db.commit()


async def get_meta(key: str) -> Optional[str]:
    """Read a meta value, or None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_meta(key: str, value: str) -> None:
    """Upsert a meta key/value."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


# ── users & hunts ─────────────────────────────────
async def ensure_user(chat_id: int, username: Optional[str]) -> None:
    """Create the user row if absent."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (chat_id, username, created_at) VALUES (?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username",
            (chat_id, username, _now()),
        )
        await db.commit()


# ── business model: referrals, plan, limits ───────
BASE_FREE_HUNTS = 1               # hunts every user starts with
SHARE_BONUS_HUNTS = 3            # one-time bonus for sharing the bot
FRIENDS_PER_HUNT = 3            # +1 hunt for each N invited friends
PRO_INVITE_THRESHOLD = 9        # invite this many friends → free PRO
PRO_DAYS_FOR_INVITES = 30
SEARCH_LIMIT_PER_DAY = 20       # FREE daily new-search cap (PRO = unlimited)


async def get_user(chat_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a user row by chat id."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


def is_pro(user: Optional[Dict[str, Any]]) -> bool:
    """Whether the user currently has an active PRO plan."""
    if not user or user.get("plan") != "pro" or not user.get("plan_until"):
        return False
    try:
        return dt.datetime.fromisoformat(user["plan_until"]) > dt.datetime.now(dt.timezone.utc)
    except ValueError:
        return False


async def grant_pro(chat_id: int, days: int) -> None:
    """Grant or extend PRO by ``days`` (from the later of now / current expiry)."""
    user = await get_user(chat_id)
    base = dt.datetime.now(dt.timezone.utc)
    if user and user.get("plan_until"):
        try:
            cur = dt.datetime.fromisoformat(user["plan_until"])
            if cur > base:
                base = cur
        except ValueError:
            pass
    until = (base + dt.timedelta(days=days)).isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE users SET plan='pro', plan_until=? WHERE chat_id=?", (until, chat_id))
        await db.commit()


async def set_referrer(chat_id: int, referrer_id: int) -> Optional[Dict[str, Any]]:
    """Attribute a new user to a referrer (once). Reward the referrer.

    Returns a reward summary for the referrer, or None if not applied (self / already set /
    referrer missing).
    """
    if chat_id == referrer_id:
        return None
    user = await get_user(chat_id)
    if user and user.get("referred_by"):
        return None
    ref = await get_user(referrer_id)
    if not ref:
        return None
    invited = (ref.get("invited_count") or 0) + 1
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE users SET referred_by=? WHERE chat_id=?", (referrer_id, chat_id))
        await db.execute("UPDATE users SET invited_count=? WHERE chat_id=?", (invited, referrer_id))
        await db.commit()
    pro_granted = 0
    if invited == PRO_INVITE_THRESHOLD:
        await grant_pro(referrer_id, PRO_DAYS_FOR_INVITES)
        pro_granted = PRO_DAYS_FOR_INVITES
    return {"invited_count": invited, "pro_days": pro_granted}


async def mark_shared(chat_id: int) -> bool:
    """Grant the one-time share bonus. Returns True only the first time."""
    user = await get_user(chat_id)
    if not user or user.get("shared"):
        return False
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE users SET shared=1 WHERE chat_id=?", (chat_id,))
        await db.commit()
    return True


async def set_phone(chat_id: int, phone: str) -> bool:
    """Store the user's phone and grant the one-time share bonus. Returns True if newly granted."""
    user = await get_user(chat_id)
    newly = bool(user) and not user.get("shared")
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("UPDATE users SET phone=?, shared=1 WHERE chat_id=?", (phone, chat_id))
        await db.commit()
    return newly


async def log_search(chat_id: int, username: Optional[str], summary: str) -> None:
    """Record what a user searched (for admin analytics)."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            "INSERT INTO search_log (chat_id, username, summary, created_at) VALUES (?,?,?,?)",
            (chat_id, username, summary, _now()),
        )
        await db.commit()


async def recent_searches(limit: int = 15) -> List[Dict[str, Any]]:
    """Most recent searches across all users."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT chat_id, username, summary, created_at FROM search_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def top_searches(limit: int = 10) -> List[Dict[str, Any]]:
    """Most frequent search summaries."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT summary, COUNT(*) c FROM search_log GROUP BY summary ORDER BY c DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def recent_users(limit: int = 15) -> List[Dict[str, Any]]:
    """Most recently registered users with key fields."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT chat_id, username, phone, plan, invited_count, created_at, is_bot "
            "FROM users WHERE COALESCE(is_bot,0)=0 ORDER BY created_at DESC LIMIT ?", (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def user_overview(chat_id: int) -> Dict[str, Any]:
    """Full profile of one user for the admin panel."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        u = await cur.fetchone()
        user = dict(u) if u else {"chat_id": chat_id}
        async def one(sql: str) -> int:
            c = await db.execute(sql, (chat_id,))
            return int((await c.fetchone())[0])
        user["hunts"] = await one("SELECT COUNT(*) FROM hunts WHERE chat_id=?")
        user["favorites"] = await one("SELECT COUNT(*) FROM favorites WHERE chat_id=?")
        user["searches"] = await one("SELECT COUNT(*) FROM search_log WHERE chat_id=?")
        cur = await db.execute(
            "SELECT summary, created_at FROM search_log WHERE chat_id=? ORDER BY created_at DESC LIMIT 5",
            (chat_id,),
        )
        user["recent_searches"] = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            "SELECT name, match_type FROM hunts WHERE chat_id=? ORDER BY created_at DESC LIMIT 5",
            (chat_id,),
        )
        user["hunt_list"] = [dict(r) for r in await cur.fetchall()]
        return user


async def find_user_by_identifier(identifier: str) -> Optional[Dict[str, Any]]:
    """Resolve a user by numeric chat_id or @username."""
    ident = identifier.strip().lstrip("@")
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if ident.lstrip("-").isdigit():
            cur = await db.execute("SELECT * FROM users WHERE chat_id=?", (int(ident),))
        else:
            cur = await db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (ident,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def new_users_count(days: int = 1) -> int:
    """How many users registered in the last N days."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(is_bot,0)=0 AND created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )
        return int((await cur.fetchone())[0])


async def active_hunt_count(chat_id: int) -> int:
    """Number of hunts the user has."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM hunts WHERE chat_id=?", (chat_id,))
        return int((await cur.fetchone())[0])


async def hunt_limit(chat_id: int) -> int:
    """Max hunts: huge for PRO, else base + share bonus + (friends // FRIENDS_PER_HUNT)."""
    user = await get_user(chat_id)
    if is_pro(user):
        return 10_000
    if not user:
        return BASE_FREE_HUNTS
    bonus = (SHARE_BONUS_HUNTS if user.get("shared") else 0)
    bonus += (user.get("invited_count") or 0) // FRIENDS_PER_HUNT
    return BASE_FREE_HUNTS + bonus


async def check_search_quota(chat_id: int) -> Dict[str, Any]:
    """Return {allowed, used, limit, pro} for today's searches (does not consume)."""
    user = await get_user(chat_id)
    if is_pro(user):
        return {"allowed": True, "used": 0, "limit": None, "pro": True}
    today = dt.date.today().isoformat()
    used = (user.get("searches_today") or 0) if user and user.get("searches_date") == today else 0
    return {"allowed": used < SEARCH_LIMIT_PER_DAY, "used": used, "limit": SEARCH_LIMIT_PER_DAY, "pro": False}


async def consume_search(chat_id: int) -> None:
    """Increment today's search counter (resetting the day if needed)."""
    today = dt.date.today().isoformat()
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT searches_today, searches_date FROM users WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        used = (row[0] or 0) if row and row[1] == today else 0
        await db.execute(
            "UPDATE users SET searches_today=?, searches_date=? WHERE chat_id=?",
            (used + 1, today, chat_id),
        )
        await db.commit()


async def add_hunt(chat_id: int, fields: Dict[str, Any]) -> int:
    """Insert a hunt for a user, returns its id."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO hunts (chat_id, name, match_type, pattern, letters_start, letters_end, "
            "digits_exact, digits_contains, digits_mask, region, vehicle_type, price_min, price_max, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                chat_id, fields.get("name"), fields["match_type"], fields.get("pattern"),
                fields.get("letters_start"), fields.get("letters_end"), fields.get("digits_exact"),
                fields.get("digits_contains"), fields.get("digits_mask"), fields.get("region"),
                fields.get("vehicle_type"), fields.get("price_min"), fields.get("price_max"), _now(),
            ),
        )
        await db.commit()
        return cur.lastrowid


def _hunt_criteria(h: Dict[str, Any], alias: str = "") -> tuple:
    """Build (where_clauses, params) for plates satisfying a hunt (no availability/time).

    ``alias`` (e.g. "p.") qualifies column names when the query joins another table.
    """
    a = alias
    where: List[str] = []
    params: List[Any] = []
    mt = h.get("match_type")
    if mt == "exact" and h.get("pattern"):
        where.append(f"{a}plate_number = ?")
        params.append(h["pattern"].replace("*", ""))
    elif mt == "starts" and h.get("letters_start"):
        where.append(f"{a}plate_number LIKE ?")
        params.append(h["letters_start"] + "%")
    elif mt == "ends" and h.get("letters_end"):
        where.append(f"{a}plate_number LIKE ?")
        params.append("%" + h["letters_end"])
    elif mt == "contains" and h.get("pattern"):
        where.append(f"{a}plate_number LIKE ?")
        params.append("%" + h["pattern"].replace("*", "") + "%")
    elif mt == "filters":
        if h.get("letters_start"):
            where.append(f"{a}letters_start = ?")
            params.append(h["letters_start"])
        if h.get("digits_exact"):
            where.append(f"{a}digits = ?")
            params.append(h["digits_exact"])
        elif h.get("digits_mask"):
            where.append(f"{a}digits LIKE ?")
            params.append(h["digits_mask"])
    elif mt == "digits_mask" and h.get("digits_mask"):
        where.append(f"{a}digits LIKE ?")
        params.append(h["digits_mask"])
    elif mt == "digits":
        if h.get("digits_exact"):
            where.append(f"{a}digits = ?")
            params.append(h["digits_exact"])
        elif h.get("digits_contains"):
            where.append(f"{a}digits LIKE ?")
            params.append("%" + h["digits_contains"] + "%")
    elif mt == "combined":
        if h.get("letters_start"):
            where.append(f"{a}plate_number LIKE ?")
            params.append(h["letters_start"] + "%")
        if h.get("letters_end"):
            where.append(f"{a}plate_number LIKE ?")
            params.append("%" + h["letters_end"])
        if h.get("digits_exact"):
            where.append(f"{a}digits = ?")
            params.append(h["digits_exact"])
    if h.get("region"):
        where.append(f"{a}region = ?")
        params.append(h["region"])
    if h.get("vehicle_type"):
        where.append(f"{a}vehicle_type = ?")
        params.append(h["vehicle_type"])
    if h.get("price_min") is not None:
        where.append(f"{a}price >= ?")
        params.append(h["price_min"])
    if h.get("price_max") is not None:
        where.append(f"{a}price <= ?")
        params.append(h["price_max"])
    if not where:
        where.append("1=0")
    return where, params


async def count_hunt_matches(h: Dict[str, Any]) -> int:
    """How many AVAILABLE plates currently satisfy this hunt."""
    where, params = _hunt_criteria(h)
    sql = "SELECT COUNT(*) FROM plates WHERE is_available=1 AND " + " AND ".join(where)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(sql, params)
        return int((await cur.fetchone())[0])


async def list_hunt_matches(h: Dict[str, Any], limit: int = 15, offset: int = 0) -> List[Dict[str, Any]]:
    """Available plates satisfying this hunt (with TSC address), cheapest first."""
    where, params = _hunt_criteria(h, alias="p.")
    sql = (
        "SELECT p.plate_number, p.region, p.tsc, p.vehicle_type, p.price, p.is_available, "
        "t.address AS tsc_address FROM plates p LEFT JOIN tsc t ON p.tsc=t.code "
        "WHERE p.is_available=1 AND " + " AND ".join(where)
        + " ORDER BY p.price IS NULL, p.price LIMIT ? OFFSET ?"
    )
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params + [limit, offset])
        return [dict(r) for r in await cur.fetchall()]


async def hunt_changes_24h(h: Dict[str, Any]) -> tuple:
    """Return (new, removed) plate counts matching the hunt in the last 24 hours."""
    where, params = _hunt_criteria(h)
    crit = " AND ".join(where)
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            f"SELECT COUNT(*) FROM plates WHERE {crit} AND first_seen_at >= datetime('now','-1 day')",
            params,
        )
        new = int((await cur.fetchone())[0])
        cur = await db.execute(
            f"SELECT COUNT(*) FROM plates WHERE {crit} AND removed_at >= datetime('now','-1 day')",
            params,
        )
        removed = int((await cur.fetchone())[0])
    return new, removed


async def get_hunt(chat_id: int, hunt_id: int) -> Optional[Dict[str, Any]]:
    """Fetch one hunt owned by the user."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM hunts WHERE id=? AND chat_id=?", (hunt_id, chat_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_hunts(chat_id: int) -> List[Dict[str, Any]]:
    """List a user's hunts."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM hunts WHERE chat_id=? ORDER BY created_at DESC", (chat_id,)
        )
        return [dict(r) for r in await cur.fetchall()]


async def toggle_hunt(chat_id: int, hunt_id: int) -> bool:
    """Flip a hunt's is_active flag. Returns True if a row was changed."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute(
            "UPDATE hunts SET is_active = 1 - is_active WHERE id=? AND chat_id=?",
            (hunt_id, chat_id),
        )
        await db.commit()
        return (cur.rowcount or 0) > 0


async def delete_hunt(chat_id: int, hunt_id: int) -> bool:
    """Delete a hunt the user owns. Returns True if deleted."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("DELETE FROM hunts WHERE id=? AND chat_id=?", (hunt_id, chat_id))
        await db.commit()
        return (cur.rowcount or 0) > 0


async def active_hunts(db: aiosqlite.Connection) -> List[Dict[str, Any]]:
    """All active hunts (for matching during a scan)."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT * FROM hunts WHERE is_active=1")
    return [dict(r) for r in await cur.fetchall()]


async def already_notified(db: aiosqlite.Connection, hunt_id: int, plate_id: int) -> bool:
    """Whether this hunt was already notified about this plate."""
    cur = await db.execute(
        "SELECT 1 FROM notified WHERE hunt_id=? AND plate_id=?", (hunt_id, plate_id)
    )
    return await cur.fetchone() is not None


async def record_notified(db: aiosqlite.Connection, hunt_id: int, plate_id: int) -> None:
    """Record that a notification was sent."""
    await db.execute(
        "INSERT OR IGNORE INTO notified (hunt_id, plate_id, created_at) VALUES (?,?,?)",
        (hunt_id, plate_id, _now()),
    )


async def get_plate(db: aiosqlite.Connection, plate_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a plate row by id."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT * FROM plates WHERE id=?", (plate_id,))
    row = await cur.fetchone()
    return dict(row) if row else None
