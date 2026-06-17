"""JSON API over the local SQLite DB — powers the mobile (Expo) app.

Read-only browsing endpoints (search, feed, collections, stats, plate detail). Favorites are
kept on-device in the MVP; accounts + monitorings/push come later. Pure script, no AI.

Run:  python -m local.api    (uvicorn on 0.0.0.0:8000)
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from local import config, db

app = FastAPI(title="Моніторинг Автономерів API", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

PAGE = 20

# Public endpoints that never require the app key.
_OPEN_PATHS = {"/health", "/open"}
# Simple in-memory per-IP rate limiter (one uvicorn worker). minute-bucket -> {ip: count}.
_rate_bucket: dict = {}
_rate_minute: list = [0]


@app.middleware("http")
async def guard(request: Request, call_next):
    """Protect the API from third-party scraping: per-IP rate limit + app API key."""
    import time

    path = request.url.path
    ip = request.client.host if request.client else "?"

    # Rate limiting (per IP, per minute).
    minute = int(time.time() // 60)
    if _rate_minute[0] != minute:
        _rate_minute[0] = minute
        _rate_bucket.clear()
    n = _rate_bucket.get(ip, 0) + 1
    _rate_bucket[ip] = n
    if n > config.API_RATE_PER_MIN:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=429, content={"detail": "Too many requests"})

    # App API key (skip health/open and the secret-protected ingest/parse-job endpoints).
    if config.API_KEY and path not in _OPEN_PATHS and not path.startswith("/viber") \
            and path not in ("/ingest", "/parse-job", "/stage"):
        if request.headers.get("x-api-key") != config.API_KEY:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


@app.on_event("startup")
async def _warm() -> None:
    """Warm the DB read cache on startup so the first request is fast; register Viber webhook."""
    try:
        await db.warm_cache()
    except Exception:  # noqa: BLE001
        pass
    if config.VIBER_TOKEN:
        try:
            import asyncio
            from local import viber
            await asyncio.to_thread(viber.set_webhook)
        except Exception as exc:  # noqa: BLE001
            print(f"[viber] set_webhook on startup failed: {exc!r}")


@app.post("/viber/webhook")
async def viber_webhook(request: Request) -> dict:
    """Receive Viber events (signature-verified) and dispatch them in the background."""
    import asyncio
    import json as _json

    from local import viber

    body = await request.body()
    sig = request.headers.get("x-viber-content-signature", "")
    if not viber.valid_signature(body, sig):
        raise HTTPException(403, "bad signature")
    try:
        event = _json.loads(body.decode("utf-8"))
    except ValueError:
        return {"status": "ignored"}
    asyncio.create_task(viber.handle(event))
    return {"status": "ok"}


@app.get("/viber/set-webhook")
async def viber_set_webhook(secret: str = "") -> dict:
    """Manually (re)register the Viber webhook (secret-protected)."""
    if not config.INGEST_SECRET or secret != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    from local import viber
    import asyncio
    return await asyncio.to_thread(viber.set_webhook)


@app.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


# Expo Go tunnel deep-link (update if the tunnel restarts).
EXPO_URL = "exp://ziwjxde-anonymous-8081.exp.direct"


@app.get("/open", response_class=HTMLResponse)
async def open_app() -> str:
    """A tappable page that redirects into Expo Go (open in the phone's browser)."""
    return f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Моніторинг Автономерів</title>
<style>body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eef1f6;
text-align:center;padding:40px 20px}}a.btn{{display:inline-block;background:#3b82f6;color:#fff;
text-decoration:none;padding:16px 28px;border-radius:14px;font-size:18px;font-weight:700;margin-top:24px}}
p{{color:#9aa4b2}}</style></head><body>
<h2>🇺🇦 Моніторинг Автономерів</h2>
<p>Натисни кнопку, щоб відкрити застосунок у Expo Go:</p>
<a class="btn" href="{EXPO_URL}">▶️ Відкрити в Expo Go</a>
<p style="margin-top:30px;font-size:13px">Якщо не відкрилось — встанови «Expo Go» з App Store і натисни ще раз.</p>
<script>setTimeout(function(){{window.location.href="{EXPO_URL}";}}, 600);</script>
</body></html>"""


@app.get("/stats")
async def stats() -> dict:
    """Aggregate stats + today's new/removed counts."""
    s = await db.get_stats()
    s["new_today"] = await db.feed_count("new", "day")
    s["removed_today"] = await db.feed_count("removed", "day")
    return s


@app.get("/meta")
async def meta(region: Optional[str] = None, vehicle_type: Optional[str] = None) -> dict:
    """Filter options: regions, vehicle types, series and prices (optionally scoped)."""
    return {
        "regions": await db.distinct_regions(),
        "vehicle_types": await db.distinct_vehicle_types(),
        "series": await db.distinct_series(region=region, vehicle_type=vehicle_type),
        "prices": await db.distinct_prices(region=region, vehicle_type=vehicle_type),
        "collections": [{"key": k, "label": v} for k, v in db.COLLECTIONS.items()],
    }


@app.get("/search")
async def search(
    query: Optional[str] = None,
    region: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    series: Optional[str] = None,
    price: Optional[float] = None,
    collection: Optional[str] = None,
    page: int = Query(0, ge=0),
) -> dict:
    """Filtered, paginated plate search."""
    kw = dict(
        query=query, region=region, vehicle_type=vehicle_type, letters_start=series,
        price_min=price, price_max=price, collection=collection,
    )
    total = await db.count_filtered(**kw)
    items = await db.search_filtered(limit=PAGE, offset=page * PAGE, **kw)
    return {"total": total, "page": page, "page_size": PAGE, "items": items}


@app.get("/feed")
async def feed(
    kind: str = "new",
    period: str = "day",
    region: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    page: int = Query(0, ge=0),
) -> dict:
    """New / removed plate events within a period."""
    total = await db.feed_count(kind, period, region, vehicle_type)
    items = await db.feed(kind, period, region, vehicle_type, limit=PAGE, offset=page * PAGE)
    return {"total": total, "page": page, "page_size": PAGE, "items": items}


@app.get("/collections")
async def collections() -> list:
    """Curated collections with counts (cached in the DB layer)."""
    return await db.collection_counts()


@app.get("/popular")
async def popular(limit: int = 15) -> list:
    """Most-favorited digit combinations."""
    return await db.popular_combos(limit)


@app.get("/plate/{plate}")
async def plate_detail(plate: str) -> dict:
    """Full details of one plate number (all locations + popularity)."""
    from local.plate import parse_plate

    locs = await db.plate_locations(plate)
    digits = parse_plate(plate).get("digits")
    return {
        "plate_number": plate,
        "locations": locs,
        "favorites_combo": await db.favorites_combo_count(digits),
        "hunts_combo": await db.hunts_combo_count(digits),
    }


@app.post("/ingest")
async def ingest(request: Request) -> dict:
    """Receive scraped data from the Mac scraper and persist it (secret-protected).

    Body: {secret, rows: [...], ok_scopes: [[region, type], ...]}.
    The scraper runs on a residential IP (Akamai); the server only stores + notifies.
    """
    if not config.INGEST_SECRET:
        raise HTTPException(503, "ingest disabled (no secret configured)")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    from local.persist import apply_scan, notify_new

    rows = body.get("rows") or []
    ok_scopes = {(s[0], s[1]) for s in (body.get("ok_scopes") or [])}
    applied = await apply_scan(rows, ok_scopes)
    notified = await notify_new(applied["new_ids"])
    return {
        "scraped": applied["scraped"], "new": len(applied["new_ids"]),
        "removed": applied["removed"], "notified": notified,
    }


@app.post("/stage")
async def stage(request: Request) -> dict:
    """Receive a collected batch into the staging queue (NOT applied to the DB yet).

    Body: {secret, rows:[...], reset?:bool, done?:bool}. The browser extension sends reset=true
    on the first scope, appends rows per scope, and done=true at the end (which marks the queue
    pending and notifies the admin). The admin then commits it (or it auto-commits after N hours).
    """
    import json as _json

    if not config.INGEST_SECRET:
        raise HTTPException(503, "staging disabled (no secret configured)")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    if body.get("reset"):
        open(config.STAGE_PATH, "w", encoding="utf-8").close()
    rows = body.get("rows") or []
    if rows:
        with open(config.STAGE_PATH, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(_json.dumps(r, ensure_ascii=False) + "\n")
    total = 0
    try:
        with open(config.STAGE_PATH, encoding="utf-8") as fh:
            total = sum(1 for _ in fh)
    except FileNotFoundError:
        total = 0
    if body.get("done"):
        import datetime as dt
        await db.set_meta("stage_ts", dt.datetime.now(dt.timezone.utc).isoformat())
        await db.set_meta("stage_pending", "1")
        await db.set_meta("stage_count", str(total))
        await db.set_meta("stage_scopes", _json.dumps(body.get("scopes") or []))
        if config.BOT_TOKEN and config.ADMIN_CHAT_ID:
            from aiogram import Bot
            from aiogram.client.default import DefaultBotProperties
            bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
            try:
                await bot.send_message(
                    config.ADMIN_CHAT_ID,
                    f"📥 <b>Отримано оновлений список</b>: {total} номерів у черзі.\n"
                    f"Оновити базу? Адмінка → <b>🔄 Оновити базу</b>.\n"
                    f"<i>Якщо не оновити вручну — автоматично за {config.STAGE_AUTOCOMMIT_HOURS} год.</i>",
                )
            except Exception:  # noqa: BLE001
                pass
            finally:
                await bot.session.close()
    return {"staged": len(rows), "total": total}


def main() -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
