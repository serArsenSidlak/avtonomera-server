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
            and path not in ("/ingest", "/parse-job", "/stage", "/collect", "/collect-html",
                             "/collector", "/autocheck/register"):
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


# Bookmarklet body (single IIFE). Placeholders @SRV@/@SEC@/@MAP@/@REGS@/@TYPES@ filled per request;
# newlines are collapsed to spaces before serving (a javascript: URL must be one line).
_COLLECTOR_JS = """
(function(){try{
var S='@SRV@',K='@SEC@';
var rs=document.querySelector('#region');
var reg=rs&&rs.selectedIndex>=0?rs.options[rs.selectedIndex].text.trim():'';
var form=rs?rs.closest('form'):document.querySelector('form');
if(!form){alert('Спершу обери регіон + Весь регіон + тип і натисни ПЕРЕГЛЯНУТИ.');return;}
var p=[],es=form.querySelectorAll('input,select,textarea');
for(var i=0;i<es.length;i++){var e=es[i];if(!e.name){continue;}if((e.type=='checkbox'||e.type=='radio')&&!e.checked){continue;}if(e.type=='submit'||e.type=='button'){continue;}p.push(encodeURIComponent(e.name)+'='+encodeURIComponent(e.value));}
var sb=form.querySelector('input[type=submit],button[type=submit]');if(sb&&sb.name){p.push(encodeURIComponent(sb.name)+'='+encodeURIComponent(sb.value||''));}
var act=form.getAttribute('action')||location.href,m=(form.getAttribute('method')||'POST').toUpperCase();
var u=act,o={method:m,credentials:'include'};
if(m=='GET'){u=act+(act.indexOf('?')<0?'?':'&')+p.join('&');}else{o.headers={'Content-Type':'application/x-www-form-urlencoded'};o.body=p.join('&');}
alert('Збираю повний список регіону, зачекай кілька секунд…');
fetch(u,o).then(function(r){return r.text();}).then(function(t){var f=document.createElement('form');f.method='POST';f.action=S+'/collect-html';f.target='_blank';f.acceptCharset='UTF-8';function a(n,v){var x=document.createElement('input');x.type='hidden';x.name=n;x.value=v;f.appendChild(x);}a('secret',K);a('region',reg);a('html',t);document.body.appendChild(f);f.submit();}).catch(function(e){alert('Помилка збору: '+e);});
}catch(e){alert('Помилка: '+e.message);}})();
"""

_COLLECTOR_PAGE = """<!doctype html><html lang=uk><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Збирач для iPhone</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eef1f6;margin:0;padding:18px;line-height:1.5}
h2{margin:0 0 12px}code{background:#1b1f27;padding:1px 5px;border-radius:5px}
textarea{width:100%;height:120px;background:#1b1f27;color:#9be7a0;border:1px solid #333;border-radius:10px;padding:10px;font-family:ui-monospace,monospace;font-size:11px}
button{background:#3b82f6;color:#fff;border:0;border-radius:10px;padding:11px 16px;font-weight:700;font-size:15px;margin-top:10px}
ol{padding-left:20px}li{margin-bottom:8px}.box{background:#161a22;border:1px solid #232834;border-radius:14px;padding:16px;margin-bottom:16px}</style>
</head><body>
<h2>📲 Збирач номерів для iPhone (Safari)</h2>
<div class=box>
<b>1. Скопіюй код закладки:</b>
<textarea id=bm readonly>@BMTEXT@</textarea>
<button onclick="var t=document.getElementById('bm');t.select();try{navigator.clipboard.writeText(t.value);}catch(e){document.execCommand('copy');}this.textContent='✅ Скопійовано';">📋 Скопіювати код</button>
</div>
<div class=box>
<b>2. Створи закладку в Safari</b>
<ol>
<li>Відкрий будь-яку сторінку → кнопка «Поділитися» <code>⬆️</code> → <b>Додати закладку</b> → Зберегти.</li>
<li>Відкрий <b>Закладки</b> → <b>Змінити</b> → обери цю нову закладку.</li>
<li>Назву постав, напр., <code>📤 Зібрати номери</code>, а в полі <b>адреси</b> зітри все і <b>встав скопійований код</b>. Готово.</li>
</ol>
</div>
<div class=box>
<b>3. Як користуватись</b>
<ol>
<li>Зайди на <code>opendata.hsc.gov.ua/check-leisure-license-plates</code>.</li>
<li>Обери <b>регіон</b> → <b>Весь регіон</b> → тип <b>(будь-який / усі)</b> → натисни <b>ПЕРЕГЛЯНУТИ</b>.</li>
<li>Коли зʼявиться таблиця з номерами — відкрий закладку <b>📤 Зібрати номери</b> (через адресний рядок або меню закладок).</li>
<li>У віконці перевір <b>регіон</b> і натисни <b>Відправити</b>. Дата збору фіксується автоматично, база оновлюється саме по цьому регіону.</li>
</ol>
</div>
<p style="color:#9aa4b2;font-size:13px">Тип ТЗ визначається автоматично по серії номера. Відстежуються і нові, і зниклі номери в межах регіону.</p>
</body></html>"""


# ── iPhone/Safari bookmarklet collector (manual per-region opendata harvest) ──
# Canonical region names (must match the `plates.region` values in the DB so removals reconcile).
_COLLECT_REGIONS = [
    "Вінницька", "Волинська", "Дніпропетровська", "Донецька", "Житомирська", "Закарпатська",
    "Запорізька", "Івано-Франківська", "Київська", "Кіровоградська", "Луганська", "Львівська",
    "Миколаївська", "Одеська", "Полтавська", "Рівненська", "Сумська", "Тернопільська",
    "Харківська", "Херсонська", "Хмельницька", "Черкаська", "Чернівецька", "Чернігівська", "м. Київ",
]
_COLLECT_TYPES = ["Легковий, вантажний", "Електромобіль", "Причіп", "Мотоцикл", "Електромотоцикл"]


async def _series_type_map() -> dict:
    """Majority series→vehicle_type map from the live DB (so the bookmarklet stays current)."""
    try:
        async with db.acquire() as con:
            rows = await con.fetch(
                "SELECT right(plate_number,2) AS s, vehicle_type AS t, count(*) AS n "
                "FROM plates WHERE plate_number ~ '..[0-9]{4}..$' GROUP BY 1,2")
    except Exception:  # noqa: BLE001
        return {}
    best: dict = {}
    for r in rows:
        s, t, n = r["s"], r["t"], r["n"]
        if s not in best or n > best[s][1]:
            best[s] = (t, n)
    return {s: tn[0] for s, tn in best.items()}


# Latin→Cyrillic for plate normalization (opendata prints plates in Latin lookalikes).
_PLATE_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                                "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})
import re as _re

_PLATE_RE = _re.compile(r"^\D{2}\d{4}\D{2}$")
_TR_RE = _re.compile(r"<tr[^>]*>(.*?)</tr>", _re.S | _re.I)
_TD_RE = _re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", _re.S | _re.I)
_TAG_RE = _re.compile(r"<[^>]+>")
_NUM_RE = _re.compile(r"[\d.,]+")


def _plate_norm(raw: str) -> str:
    return _re.sub(r"[\s\-]", "", raw or "").strip().upper().translate(_PLATE_LAT2CYR)


def _vtype_server(plate: str, smap: dict) -> str:
    """Vehicle type from plate series (last 2 letters): exact DB map, then scheme rule (Додаток 5)."""
    s = plate[-2:]
    if s in smap:
        return smap[s]
    a, b = s[:1], s[1:2]
    if a == "F":
        return "Причіп"
    if a == "Х" and b in "FGJLNRSUV":
        return "Причіп"
    if a in ("J", "L"):
        return "Мотоцикл"
    if a == "R":
        return "Електромотоцикл"
    if a in ("U", "Y", "Z"):
        return "Електромобіль"
    return "Легковий, вантажний"


def _canon_region(label: str) -> str:
    """Map an opendata region label to the canonical DB region name."""
    s = _re.sub(r"\s*область$", "", (label or "").strip(), flags=_re.I).strip()
    low = s.lower().replace(".", "").replace(" ", "")
    if low in ("київ", "мкиїв", "містокиїв"):
        return "м. Київ"
    if s in _COLLECT_REGIONS:
        return s
    for c in _COLLECT_REGIONS:  # tolerate minor wording differences
        if c == s or c.startswith(s) or s.startswith(c):
            return c
    return s


def _parse_plate_html(html: str, smap: dict) -> list:
    """Extract plate rows from an opendata results HTML page (server-side, stdlib only)."""
    def _txt(x: str) -> str:
        return _TAG_RE.sub(" ", x).replace("&nbsp;", " ").replace("&amp;", "&").strip()

    rows, seen = [], set()
    for tr in _TR_RE.findall(html or ""):
        tds = _TD_RE.findall(tr)
        if len(tds) < 3:
            continue
        plate = _plate_norm(_txt(tds[0]))
        if not _PLATE_RE.match(plate):
            continue
        tsc = _txt(tds[2]) or None
        key = (plate, tsc)
        if key in seen:
            continue
        seen.add(key)
        pm = _NUM_RE.search(_txt(tds[1]).replace(" ", ""))
        price = None
        if pm:
            try:
                price = float(pm.group(0).replace(",", "."))
            except ValueError:
                price = None
        rows.append({"plate_number": plate, "price": price, "tsc": tsc,
                     "vehicle_type": _vtype_server(plate, smap)})
    return rows


@app.post("/collect-html")
async def collect_html(request: Request):
    """Receive a RAW opendata results page from the tiny bookmarklet; parse + classify + ingest here.

    Body (JSON or x-www-form-urlencoded): {secret, region, html}. The server extracts all plates
    (full list, not the visible 10/page), assigns vehicle_type by series, and applies the snapshot
    for that region (new + removed). Keeps the bookmarklet tiny enough to fit in a Safari bookmark.
    """
    import json as _json

    if not config.INGEST_SECRET:
        raise HTTPException(503, "collect disabled (no secret configured)")
    ctype = request.headers.get("content-type", "")
    is_form = "application/json" not in ctype
    if is_form:
        from urllib.parse import parse_qs

        raw = (await request.body()).decode("utf-8", "replace")
        q = parse_qs(raw, keep_blank_values=True)
        secret = (q.get("secret") or [""])[0]
        region_label = (q.get("region") or [""])[0]
        html = (q.get("html") or [""])[0]
    else:
        body = await request.json()
        secret = body.get("secret", "")
        region_label = body.get("region", "")
        html = body.get("html", "")
    if secret != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")

    smap = await _series_type_map()
    region = _canon_region(region_label)
    rows = _parse_plate_html(html, smap)
    for r in rows:
        r["region"] = region

    if not rows:
        msg = ("Не знайшов жодного номера в сторінці. Переконайся, що відкрита таблиця "
               "результатів (Регіон → Весь регіон → ПЕРЕГЛЯНУТИ).")
        if is_form:
            return HTMLResponse(
                "<!doctype html><meta charset=utf-8><body style='font-family:-apple-system,sans-serif;"
                f"padding:28px;background:#0f1115;color:#eef1f6'><h2>⚠️ 0 номерів</h2><p>{msg}</p></body>")
        return {"region": region, "scraped": 0, "new": 0, "removed": 0, "notified": 0, "note": msg}

    from local.persist import apply_scan, notify_new

    present = sorted({r["vehicle_type"] for r in rows})
    ok_scopes = {(region, t) for t in present}
    applied = await apply_scan(rows, ok_scopes)
    notified = await notify_new(applied["new_ids"])
    result = {"region": region, "scraped": applied["scraped"], "new": len(applied["new_ids"]),
              "removed": applied["removed"], "notified": notified}
    if not is_form:
        return result
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:-apple-system,sans-serif;padding:28px;background:#0f1115;color:#eef1f6'>"
        f"<h2>✅ {region}</h2>"
        f"<p>Усього в знімку: <b>{result['scraped']}</b><br>Нових: <b>{result['new']}</b><br>"
        f"Знято (зникли): <b>{result['removed']}</b></p>"
        "<p style='color:#9aa4b2'>Можеш закрити цю вкладку і повернутись до сайту.</p></body>")


@app.post("/collect")
async def collect(request: Request):
    """Receive a manually-harvested region snapshot from the iPhone bookmarklet and apply it.

    Accepts JSON (fetch) OR form-encoded `payload` (CSP fallback that posts into a new tab).
    Body: {secret, rows:[{plate_number,price,tsc,region,vehicle_type}], ok_scopes:[[region,type]]}.
    """
    import json as _json

    if not config.INGEST_SECRET:
        raise HTTPException(503, "collect disabled (no secret configured)")
    ctype = request.headers.get("content-type", "")
    is_form = "application/json" not in ctype
    if is_form:
        # Parse x-www-form-urlencoded manually (avoids the python-multipart dependency).
        from urllib.parse import parse_qs

        raw = (await request.body()).decode("utf-8", "replace")
        payload = (parse_qs(raw).get("payload") or ["{}"])[0]
        body = _json.loads(payload)
    else:
        body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    from local.persist import apply_scan, notify_new

    rows = body.get("rows") or []
    ok_scopes = {(s[0], s[1]) for s in (body.get("ok_scopes") or [])}
    applied = await apply_scan(rows, ok_scopes)
    notified = await notify_new(applied["new_ids"])
    result = {"scraped": applied["scraped"], "new": len(applied["new_ids"]),
              "removed": applied["removed"], "notified": notified}
    if not is_form:
        return result
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:-apple-system,sans-serif;padding:28px;background:#0f1115;color:#eef1f6'>"
        f"<h2>✅ Відправлено в базу</h2><p>Регіон оброблено.</p>"
        f"<p>Нових: <b>{result['new']}</b><br>Знято (зникли): <b>{result['removed']}</b><br>"
        f"Усього в знімку: <b>{result['scraped']}</b></p>"
        "<p style='color:#9aa4b2'>Можеш закрити цю вкладку і повернутись до сайту.</p></body>")


@app.get("/collector")
async def collector_page(k: str = ""):
    """Token-gated install page: the iPhone Safari bookmarklet + setup steps (k = app key)."""
    import json as _json

    if not config.API_KEY or k != config.API_KEY:
        raise HTTPException(403, "forbidden")
    smap = await _series_type_map()
    one_line = " ".join(ln.strip() for ln in _COLLECTOR_JS.strip().splitlines())
    srv = (config.SERVER_INGEST_URL.rstrip("/") or "https://34.123.136.171.nip.io").rstrip("/")
    if srv.endswith("/ingest"):
        srv = srv[: -len("/ingest")]
    js = (one_line
          .replace("@SRV@", srv)
          .replace("@SEC@", config.INGEST_SECRET)
          .replace("@MAP@", _json.dumps(smap, ensure_ascii=False))
          .replace("@REGS@", _json.dumps(_COLLECT_REGIONS, ensure_ascii=False))
          .replace("@TYPES@", _json.dumps(_COLLECT_TYPES, ensure_ascii=False)))
    bookmarklet = "javascript:" + js
    page = _COLLECTOR_PAGE.replace("@BM@", _html_attr(bookmarklet)).replace("@BMTEXT@", _html_text(bookmarklet))
    return HTMLResponse(page)


def _html_attr(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _html_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.post("/autocheck/register")
async def autocheck_register(request: Request) -> dict:
    """The AutoCheck PC-agent registers its current Cloudflare tunnel URL here (secret-protected)."""
    import datetime as dt

    if not config.INGEST_SECRET:
        raise HTTPException(503, "disabled (no secret configured)")
    body = await request.json()
    if body.get("secret") != config.INGEST_SECRET:
        raise HTTPException(403, "bad secret")
    url = (body.get("url") or "").strip().rstrip("/")
    if not url.startswith("https://"):
        raise HTTPException(400, "bad url")
    await db.set_meta("autocheck_url", url)
    await db.set_meta("autocheck_ts", dt.datetime.now(dt.timezone.utc).isoformat())
    return {"ok": True, "url": url}


@app.get("/autocheck/lookup")
async def autocheck_lookup(plate: str = "", vin: str = ""):
    """Proxy a vehicle lookup to the registered PC-agent tunnel (so app/bot use a stable URL).

    Behind the app API key (middleware). Returns {found, vehicle, history} or {found:false}.
    """
    import asyncio
    import json as _json
    from urllib.parse import quote

    url = await db.get_meta("autocheck_url")
    if not url:
        return {"found": False, "offline": True, "note": "AutoCheck-агент не підключений"}
    q = f"plate={quote(plate)}" if plate else (f"vin={quote(vin)}" if vin else "")
    if not q:
        raise HTTPException(400, "plate or vin required")

    def _call():
        req = urllib.request.Request(f"{url}/lookup?{q}", headers={"x-secret": config.INGEST_SECRET})
        with urllib.request.urlopen(req, timeout=20) as r:
            return _json.loads(r.read().decode("utf-8"))

    try:
        return await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "offline": True, "note": f"агент недоступний ({exc})"}


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


BOT_USERNAME = "nomer_na_avto_bot"


async def _account(request: Request) -> int:
    """Resolve the linked Telegram chat_id from the X-App-Token header (401 if not linked)."""
    token = request.headers.get("x-app-token", "")
    chat_id = await db.token_chat(token) if token else None
    if not chat_id:
        raise HTTPException(401, "not linked")
    return int(chat_id)


@app.post("/app/link/start")
async def app_link_start() -> dict:
    """Begin linking the app to a Telegram account: returns a code, token and a bot deep-link."""
    import secrets
    code = secrets.token_urlsafe(6)
    token = secrets.token_urlsafe(24)
    await db.link_create(code, token)
    return {"code": code, "token": token,
            "deep_link": f"https://t.me/{BOT_USERNAME}?start=link_{code}"}


@app.get("/app/link/status")
async def app_link_status(code: str) -> dict:
    """Poll whether the link code has been confirmed in the bot."""
    st = await db.link_status(code)
    if not st:
        return {"status": "unknown"}
    return {"status": st.get("status"), "linked": st.get("status") == "linked"}


@app.get("/app/me")
async def app_me(request: Request) -> dict:
    """Return the linked account + its synced favorites/monitorings counts."""
    chat_id = await _account(request)
    user = await db.get_user(chat_id)
    return {
        "chat_id": chat_id,
        "username": (user or {}).get("username"),
        "favorites": len(await db.list_favorites(chat_id)),
        "monitorings": len(await db.list_hunts(chat_id)),
    }


@app.get("/app/favorites")
async def app_favorites(request: Request) -> dict:
    """List the account's favorite plate numbers (synced with the bot)."""
    chat_id = await _account(request)
    return {"items": await db.list_favorites(chat_id)}


@app.post("/app/favorites/toggle")
async def app_fav_toggle(request: Request) -> dict:
    """Add/remove a plate from the account's favorites (synced with the bot)."""
    chat_id = await _account(request)
    body = await request.json()
    plate = (body.get("plate") or "").strip()
    if not plate:
        raise HTTPException(400, "no plate")
    if await db.is_favorite(chat_id, plate):
        await db.remove_favorite(chat_id, plate)
        return {"favorite": False}
    await db.add_favorite(chat_id, plate)
    return {"favorite": True}


@app.post("/app/account/anon")
async def app_account_anon() -> dict:
    """Create an anonymous app account so the app works fully without any Telegram link."""
    import secrets
    token = secrets.token_urlsafe(24)
    chat_id = -(3_000_000_000 + secrets.randbelow(1_000_000_000))  # synthetic, app-only namespace
    await db.ensure_user(chat_id, None)
    await db.create_anon_account(token, chat_id)
    return {"token": token}


@app.get("/app/monitorings")
async def app_monitorings(request: Request) -> dict:
    """List the account's monitorings (hunts) with current match counts."""
    chat_id = await _account(request)
    hunts = await db.list_hunts(chat_id)
    out = []
    for h in hunts:
        out.append({
            "id": h.get("id"), "name": h.get("name") or h.get("pattern"),
            "region": h.get("region"), "vehicle_type": h.get("vehicle_type"),
            "matches": await db.count_hunt_matches(h),
        })
    return {"items": out}


@app.post("/app/monitorings/create")
async def app_monitor_create(request: Request) -> dict:
    """Create a monitoring in-app (independent of Telegram). Body: {query, region, vehicle_type}."""
    from local.plate import to_search_like

    chat_id = await _account(request)
    body = await request.json()
    q = (body.get("query") or "").strip().upper()
    region = body.get("region") or None
    vtype = body.get("vehicle_type") or None
    fields = {"match_type": "filters", "region": region, "vehicle_type": vtype}
    if q:
        mode, pattern = to_search_like(q)
        if mode == "digits":
            if "_" in pattern:
                fields["digits_mask"] = pattern
            else:
                fields["digits_exact"] = pattern
        else:
            from local.plate import normalize_plate
            letters = normalize_plate(q)
            import re
            letters = re.sub(r"\d", "", letters)
            if letters:
                fields["letters_start"] = letters[:2]
    label = q or ((region or "всі") + " · " + (vtype or "всі"))
    fields["pattern"] = label
    fields["name"] = label
    hid = await db.add_hunt(chat_id, fields)
    return {"id": hid, "matches": await db.count_hunt_matches(fields)}


@app.post("/app/monitorings/delete")
async def app_monitor_delete(request: Request) -> dict:
    """Delete one of the account's monitorings."""
    chat_id = await _account(request)
    body = await request.json()
    ok = await db.delete_hunt(chat_id, int(body.get("id")))
    return {"deleted": ok}


@app.post("/app/merge")
async def app_merge(request: Request) -> dict:
    """Merge an old (anonymous) account's data into the current one (on linking)."""
    to_chat = await _account(request)
    body = await request.json()
    old_token = body.get("old_token") or ""
    from_chat = await db.token_chat(old_token) if old_token else None
    if from_chat and from_chat != to_chat:
        await db.merge_account(from_chat, to_chat)
        db.invalidate_cache()
    return {"merged": bool(from_chat)}


def main() -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
