"""Автономера — Windows control-panel agent (opendata.hsc.gov.ua scraper).

Opens a local web panel in the browser. From there you can scrape any region (or all),
watch progress + per-region last-scan status (ok/fail + time), and push updates to the
server's DB selectively (per region) or all at once. Supports an HTTP/SOCKS proxy so you
can present a different IP if the portal blocks yours.

The portal returns the WHOLE region (all TSC + all vehicle types) in a single request when
"Весь регіон" + type "all" is selected — so it's ~1 request per region.

Run (Windows): just launch the .exe → the panel opens at http://127.0.0.1:8732
"""
from __future__ import annotations

import asyncio
import json
import os

# Pin Playwright's browser dir so the bundled build downloads/launches Chromium consistently.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ms-playwright"
)

import random
import re
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from agent_config import INGEST_SECRET, SERVER_URL

PAGE_URL = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PORT = 8732

_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                          "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})
_PRICE_RE = re.compile(r"[\d.,]+")


def _ensure_chromium() -> None:
    """Download Chromium into PLAYWRIGHT_BROWSERS_PATH on first run (frozen-exe safe).

    A PyInstaller build bundles the Playwright driver (``--collect-all playwright``) but NOT the
    browser binary, so on a fresh PC the first launch must fetch it. Subsequent runs find it
    already present and skip. Safe to call every start.
    """
    try:
        import subprocess
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        cmd = compute_driver_executable()
        cmd = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        print("Перевіряю браузер (перший запуск може якийсь час качати Chromium)…", flush=True)
        subprocess.run([*cmd, "install", "chromium"], env=get_driver_env(), check=False)
    except Exception as exc:  # noqa: BLE001
        print(f"(підготовка браузера) {exc}", flush=True)


def _norm_region(s: str) -> str:
    """Normalise a region label for fuzzy matching (drop 'область', spaces, punctuation, case)."""
    s = (s or "").lower().replace("область", "")
    return re.sub(r"[^a-zа-яіїєґ0-9]", "", s)

# Shared state (guarded by _lock).
_lock = threading.Lock()
STATE = {
    "regions": {},      # name -> {status, time, count}
    "order": [],        # region display order
    "values": {},       # name -> portal <option> value
    "cache": {},        # name -> rows pending send
    "scanning": False,
    "progress": "",
    "proxy": "",
    "loaded": False,
}


def _log(msg):
    with _lock:
        STATE["progress"] = msg
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize_plate(raw):
    return re.sub(r"[\s\-]", "", raw or "").strip().upper().translate(_LAT2CYR)


# Vehicle types we track (must match DB labels). opendata returns the whole region without a type
# column, so we recover the type from the plate's series (last 2 letters) per Додаток 5 — special
# categories use Latin series letters (F=причіп, J/L=мото, R=електромото, U/Y/Z=електро), regular
# cars use Cyrillic. SERIES_TYPE below is the exact majority mapping derived from our own DB
# (197 series, 99.86% pure); _vtype falls back to the scheme rule for any unseen series.
ALL_TYPES = ["Легковий, вантажний", "Електромобіль", "Причіп", "Мотоцикл", "Електромотоцикл"]
SERIES_TYPE = {"ВА": "Легковий, вантажний", "ІВ": "Легковий, вантажний", "LК": "Мотоцикл", "ОС": "Легковий, вантажний", "ОА": "Легковий, вантажний", "МЕ": "Легковий, вантажний", "YD": "Електромобіль", "JF": "Мотоцикл", "НА": "Легковий, вантажний", "НК": "Легковий, вантажний", "ТВ": "Легковий, вантажний", "АС": "Легковий, вантажний", "ZА": "Електромобіль", "ВІ": "Легковий, вантажний", "РТ": "Легковий, вантажний", "ХJ": "Причіп", "РА": "Легковий, вантажний", "КЕ": "Легковий, вантажний", "НМ": "Легковий, вантажний", "JU": "Мотоцикл", "СС": "Легковий, вантажний", "YК": "Електромобіль", "ХL": "Причіп", "ВХ": "Легковий, вантажний", "МО": "Легковий, вантажний", "ТН": "Легковий, вантажний", "FN": "Причіп", "ХС": "Легковий, вантажний", "JС": "Мотоцикл", "YS": "Електромобіль", "ZG": "Електромобіль", "КМ": "Легковий, вантажний", "YJ": "Електромобіль", "ВВ": "Легковий, вантажний", "МВ": "Легковий, вантажний", "ІХ": "Легковий, вантажний", "ЕА": "Легковий, вантажний", "ХА": "Легковий, вантажний", "ZZ": "Електромобіль", "ЕТ": "Легковий, вантажний", "СН": "Легковий, вантажний", "ZЕ": "Електромобіль", "JD": "Мотоцикл", "НЕ": "Легковий, вантажний", "RА": "Електромотоцикл", "ІР": "Легковий, вантажний", "ІК": "Легковий, вантажний", "ВС": "Легковий, вантажний", "НТ": "Легковий, вантажний", "YY": "Електромобіль", "ЕВ": "Легковий, вантажний", "НО": "Легковий, вантажний", "ТА": "Легковий, вантажний", "АЕ": "Легковий, вантажний", "ZF": "Електромобіль", "КВ": "Легковий, вантажний", "СХ": "Легковий, вантажний", "YС": "Електромобіль", "ІС": "Легковий, вантажний", "АХ": "Легковий, вантажний", "UІ": "Електромобіль", "АН": "Легковий, вантажний", "ВМ": "Легковий, вантажний", "АВ": "Легковий, вантажний", "JS": "Мотоцикл", "ОО": "Легковий, вантажний", "FR": "Причіп", "ХF": "Причіп", "YО": "Електромобіль", "РС": "Легковий, вантажний", "ОТ": "Легковий, вантажний", "YВ": "Електромобіль", "YU": "Електромобіль", "ХІ": "Легковий, вантажний", "ЕР": "Легковий, вантажний", "ХU": "Причіп", "КН": "Легковий, вантажний", "ХS": "Причіп", "JJ": "Мотоцикл", "FJ": "Причіп", "ХВ": "Легковий, вантажний", "ОХ": "Легковий, вантажний", "ЕМ": "Легковий, вантажний", "ХV": "Причіп", "РІ": "Легковий, вантажний", "РН": "Легковий, вантажний", "ZD": "Електромобіль", "YF": "Електромобіль", "UG": "Електромобіль", "СО": "Легковий, вантажний", "FS": "Причіп", "ТР": "Легковий, вантажний", "UН": "Електромобіль", "КК": "Легковий, вантажний", "ZС": "Електромобіль", "YN": "Електромобіль", "JВ": "Мотоцикл", "МА": "Легковий, вантажний", "UF": "Електромобіль", "КС": "Легковий, вантажний", "YР": "Електромобіль", "АР": "Легковий, вантажний", "КА": "Легковий, вантажний", "ТК": "Легковий, вантажний", "FG": "Причіп", "YТ": "Електромобіль", "СМ": "Легковий, вантажний", "МІ": "Легковий, вантажний", "ХЕ": "Легковий, вантажний", "ВК": "Легковий, вантажний", "НР": "Легковий, вантажний", "ХN": "Причіп", "ВН": "Легковий, вантажний", "СР": "Легковий, вантажний", "LR": "Мотоцикл", "АТ": "Легковий, вантажний", "ІО": "Легковий, вантажний", "ZВ": "Електромобіль", "ЕХ": "Легковий, вантажний", "РХ": "Легковий, вантажний", "ТО": "Легковий, вантажний", "FY": "Причіп", "СА": "Легковий, вантажний", "YL": "Електромобіль", "КТ": "Легковий, вантажний", "YG": "Електромобіль", "YR": "Електромобіль", "КР": "Легковий, вантажний", "НХ": "Легковий, вантажний", "СІ": "Легковий, вантажний", "ЕІ": "Легковий, вантажний", "JЕ": "Мотоцикл", "ІА": "Легковий, вантажний", "МХ": "Легковий, вантажний", "ОМ": "Легковий, вантажний", "ТЕ": "Легковий, вантажний", "LJ": "Мотоцикл", "ІІ": "Легковий, вантажний", "ІЕ": "Легковий, вантажний", "НВ": "Легковий, вантажний", "НС": "Легковий, вантажний", "YН": "Електромобіль", "ЕС": "Легковий, вантажний", "ЕК": "Легковий, вантажний", "YІ": "Електромобіль", "ОР": "Легковий, вантажний", "ХО": "Легковий, вантажний", "МС": "Легковий, вантажний", "YА": "Електромобіль", "ЕН": "Легковий, вантажний", "ВЕ": "Легковий, вантажний", "РК": "Легковий, вантажний", "FU": "Причіп", "РВ": "Легковий, вантажний", "ХТ": "Легковий, вантажний", "ЕО": "Легковий, вантажний", "РО": "Легковий, вантажний", "АК": "Легковий, вантажний", "ВО": "Легковий, вантажний", "РМ": "Легковий, вантажний", "ІТ": "Легковий, вантажний", "АІ": "Легковий, вантажний", "АА": "Мотоцикл", "КХ": "Легковий, вантажний", "ХН": "Легковий, вантажний", "YХ": "Електромобіль", "ІН": "Легковий, вантажний", "СВ": "Легковий, вантажний", "UJ": "Електромобіль", "КІ": "Легковий, вантажний", "ІМ": "Легковий, вантажний", "ХG": "Причіп", "КО": "Легковий, вантажний", "РЕ": "Легковий, вантажний", "СТ": "Легковий, вантажний", "МТ": "Легковий, вантажний", "АМ": "Легковий, вантажний", "ТС": "Легковий, вантажний", "LО": "Мотоцикл", "СК": "Легковий, вантажний", "ЕЕ": "Легковий, вантажний", "НН": "Легковий, вантажний", "АО": "Легковий, вантажний", "JА": "Мотоцикл", "МН": "Легковий, вантажний", "ХR": "Причіп", "YМ": "Електромобіль", "ТМ": "Легковий, вантажний", "ВТ": "Легковий, вантажний", "НІ": "Легковий, вантажний", "ТІ": "Легковий, вантажний", "МК": "Легковий, вантажний", "СЕ": "Легковий, вантажний", "ХМ": "Легковий, вантажний", "МР": "Легковий, вантажний", "ТХ": "Легковий, вантажний", "YЕ": "Електромобіль"}


def _vtype(plate):
    """Vehicle type from the plate series (last 2 letters). Exact DB map, then scheme rule."""
    s = (plate or "")[-2:]
    if s in SERIES_TYPE:
        return SERIES_TYPE[s]
    a, b = (s[:1], s[1:2])
    if a == "F":
        return "Причіп"
    if a == "Х" and b in "FGJLNRSUV":  # Х (Cyrillic) + Latin second letter = trailer
        return "Причіп"
    if a in ("J", "L"):
        return "Мотоцикл"
    if a == "R":
        return "Електромотоцикл"
    if a in ("U", "Y", "Z"):
        return "Електромобіль"
    return "Легковий, вантажний"


def _price(text):
    m = _PRICE_RE.search((text or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _parse_table(html, region):
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        head = t.find("tr")
        if head and "Номерний" in head.get_text():
            table = t
            break
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        plate = tds[0].get_text(strip=True)
        if not plate or "Номерний" in plate:
            continue
        # opendata columns: Номерний знак · Ціна · Місце (ТСЦ). Type not in the table → derive
        # it from the series (last 2 letters) via _vtype, so 1 request/region is enough.
        num = normalize_plate(plate)
        rows.append({"plate_number": num, "price": _price(tds[1].get_text(strip=True)),
                     "tsc": tds[2].get_text(strip=True) or None, "region": region,
                     "vehicle_type": _vtype(num)})
    return rows


def _proxy_arg(proxy):
    """Parse 'host:port' or 'host:port:user:pass' (or scheme://...) into Playwright proxy dict."""
    p = (proxy or "").strip()
    if not p:
        return None
    scheme = "http"
    if "://" in p:
        scheme, p = p.split("://", 1)
    parts = p.split(":")
    if len(parts) >= 4:
        return {"server": f"{scheme}://{parts[0]}:{parts[1]}", "username": parts[2], "password": ":".join(parts[3:])}
    if len(parts) >= 2:
        return {"server": f"{scheme}://{parts[0]}:{parts[1]}"}
    return {"server": f"{scheme}://{p}"}


async def _human(page, secs):
    for i in range(secs):
        await page.mouse.move(random.randint(50, 1200), random.randint(50, 650), steps=random.randint(3, 9))
        await asyncio.sleep(1)


async def _options(page, css_id):
    raw = await page.evaluate(
        "(id)=>{const s=document.querySelector(id);return s?Array.from(s.options)"
        ".map(o=>[o.value,o.textContent.trim()]):[]}", css_id)
    return [(v, l) for v, l in raw if v and v not in ("0", "-1") and l]


async def _scrape(region_names, proxy):
    """Scrape the given regions (names). Per region: «Весь регіон» × кожен тип ТЗ
    (перебираємо типи, бо opendata не маркує тип у відповіді — так зберігаємо vehicle_type)."""
    launch = {"headless": False, "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                                          "--start-maximized"]}
    pr = _proxy_arg(proxy)
    if pr:
        launch["proxy"] = pr
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch)
        ctx = await browser.new_context(locale="uk-UA", timezone_id="Europe/Kyiv", user_agent=USER_AGENT,
                                        viewport={"width": 1366, "height": 768})
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = await ctx.new_page()
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            for _ in range(8):
                await _human(page, 4)
                if await page.evaluate("()=>document.querySelectorAll('select').length") > 0:
                    break
                await asyncio.sleep(3)
            all_regions = await _options(page, "#region")
            with _lock:
                STATE["values"] = {n: v for v, n in all_regions}
                if not STATE["order"]:
                    STATE["order"] = [n for _, n in all_regions]
                    for n in STATE["order"]:
                        STATE["regions"].setdefault(n, {"status": "—", "time": "", "count": 0})
                STATE["loaded"] = True
            targets = region_names or [n for _, n in all_regions]
            for name in targets:
                rv = STATE["values"].get(name)
                if not rv:  # fuzzy fallback: match e.g. "Львівська" → "Львівська область"
                    nn = _norm_region(name)
                    for dn, dv in STATE["values"].items():
                        dnn = _norm_region(dn)
                        if nn and (nn == dnn or nn in dnn or dnn in nn):
                            rv, name = dv, dn
                            break
                if not rv:
                    _log(f"{name}: немає такої області у списку порталу")
                    continue
                _log(f"Парсю {name}…")
                try:
                    await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_selector("#region", timeout=15000)
                    await page.select_option("#region", rv)
                    await asyncio.sleep(random.uniform(0.4, 1.0))
                    for sel in ("a.close_link", "text=Залишитись на основному сайті", "button.close"):
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            try:
                                await loc.click(timeout=2500)
                                break
                            except Exception:
                                pass
                    # One request: whole region, all types. Type is recovered per-plate from series.
                    await page.select_option("#tsc", "Весь регіон")
                    await page.select_option("#type_venichle", "all")
                    await asyncio.sleep(random.uniform(0.4, 1.0))
                    async with page.expect_response(
                        lambda r: r.request.method == "POST" and "check-leisure-license-plates" in r.url,
                        timeout=45000) as ri:
                        await page.locator("input[type=submit]").last.click(timeout=15000, no_wait_after=True)
                    body = await (await ri.value).text()
                    if "Номерний" not in body:
                        raise RuntimeError("blocked / no table")
                    rows = _parse_table(body, name)
                    with _lock:
                        STATE["cache"][name] = rows
                        STATE["regions"][name] = {"status": "ok", "count": len(rows),
                                                  "time": time.strftime("%d.%m.%Y %H:%M")}
                    _log(f"{name}: {len(rows)} номерів ✅")
                except Exception as exc:  # noqa: BLE001
                    with _lock:
                        STATE["regions"][name] = {"status": "fail", "count": 0,
                                                  "time": time.strftime("%d.%m.%Y %H:%M")}
                    _log(f"{name}: невдало ❌ ({exc})")
                await asyncio.sleep(random.uniform(1.0, 2.0))
        finally:
            await browser.close()


def _run_scan(region_names):
    with _lock:
        if STATE["scanning"]:
            return
        STATE["scanning"] = True
        proxy = STATE["proxy"]
    try:
        asyncio.run(_scrape(region_names, proxy))
    except Exception as exc:  # noqa: BLE001
        _log(f"Помилка скану: {exc}")
    finally:
        with _lock:
            STATE["scanning"] = False
        _log("Готово.")


def _send(region_names):
    """Push cached rows for the given regions to the server DB (/ingest)."""
    total = 0
    for name in region_names:
        with _lock:
            rows = STATE["cache"].get(name) or []
        if not rows:
            continue
        # Region snapshot is complete (all types) → reconcile removals across every type, so a
        # type that dropped to zero this scan also gets its vanished plates marked removed.
        scopes = [[name, t] for t in ALL_TYPES]
        payload = json.dumps({"secret": INGEST_SECRET, "rows": rows,
                              "ok_scopes": scopes}).encode("utf-8")
        req = urllib.request.Request(SERVER_URL.rstrip("/") + "/ingest", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                resp = r.read().decode("utf-8")
            total += len(rows)
            _log(f"📤 {name}: надіслано {len(rows)} → {resp[:120]}")
        except Exception as exc:  # noqa: BLE001
            _log(f"📤 {name}: помилка надсилання ({exc})")
    _log(f"Відправлено всього ~{total} номерів.")
    return total


PANEL_HTML = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Автономера — парсер</title>
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eef1f6;margin:0;padding:18px}
h2{margin:0 0 12px}button{background:#3b82f6;color:#fff;border:0;border-radius:10px;padding:9px 14px;font-weight:700;cursor:pointer;margin:3px}
button.g{background:#16a34a}button.gray{background:#374151}
input{background:#1b1f27;color:#eef1f6;border:1px solid #333;border-radius:8px;padding:8px;width:320px}
table{width:100%;border-collapse:collapse;margin-top:12px}td,th{padding:7px 8px;border-bottom:1px solid #232834;text-align:left;font-size:14px}
.ok{color:#22c55e}.fail{color:#ef4444}.muted{color:#9aa4b2}#prog{margin:10px 0;color:#7dd3fc;min-height:20px}
.bar{background:#1b1f27;border-radius:10px;padding:12px;margin-bottom:12px}
</style></head><body>
<h2>🚗 Автономера — парсер opendata</h2>
<div class="bar">
  <button onclick="scanAll()">▶️ Парсити ВСІ області</button>
  <button class="g" onclick="sendAll()">📤 Відправити ВСІ в базу</button>
  <span id="status" class="muted"></span>
  <div style="margin-top:8px"><input id="proxy" placeholder="проксі host:port[:user:pass] (необовʼязково)" onchange="setProxy()"></div>
  <div id="prog"></div>
</div>
<table id="tbl"><thead><tr><th>Область</th><th>Останній скан</th><th>Номерів</th><th>Дії</th></tr></thead><tbody></tbody></table>
<script>
async function j(u,m,b){const r=await fetch(u,{method:m||'GET',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});return r.json();}
function scanAll(){j('/api/scan','POST',{region:'all'});}
function sendAll(){j('/api/send','POST',{region:'all'});}
function scanOne(n){j('/api/scan','POST',{region:n});}
function sendOne(n){j('/api/send','POST',{region:n});}
function setProxy(){j('/api/proxy','POST',{proxy:document.getElementById('proxy').value});}
async function tick(){
  const s=await j('/api/state');
  document.getElementById('status').textContent=s.scanning?'⏳ Сканую…':'';
  document.getElementById('prog').textContent=s.progress||'';
  const tb=document.querySelector('#tbl tbody');tb.innerHTML='';
  for(const n of s.order){const r=s.regions[n]||{};
    const st=r.status==='ok'?'<span class=ok>✅ вдало</span>':r.status==='fail'?'<span class=fail>❌ невдало</span>':'<span class=muted>—</span>';
    tb.innerHTML+=`<tr><td>${n}</td><td>${st} <span class=muted>${r.time||''}</span></td><td>${r.count||0}</td>`+
      `<td><button onclick="scanOne('${n}')">Парсити</button><button class=g onclick="sendOne('${n}')">📤 В базу</button></td></tr>`;}
}
setInterval(tick,1500);tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(PANEL_HTML.encode("utf-8"))
        elif self.path == "/api/state":
            with _lock:
                self._json({"regions": STATE["regions"], "order": STATE["order"],
                            "scanning": STATE["scanning"], "progress": STATE["progress"]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
        if self.path == "/api/scan":
            reg = body.get("region")
            names = None if reg == "all" else [reg]
            threading.Thread(target=_run_scan, args=(names,), daemon=True).start()
            self._json({"started": True})
        elif self.path == "/api/send":
            reg = body.get("region")
            with _lock:
                names = list(STATE["cache"].keys()) if reg == "all" else [reg]
            threading.Thread(target=_send, args=(names,), daemon=True).start()
            self._json({"started": True})
        elif self.path == "/api/proxy":
            with _lock:
                STATE["proxy"] = (body.get("proxy") or "").strip()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def main():
    if not INGEST_SECRET:
        print("WARNING: INGEST_SECRET empty — sending to DB will fail.", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Панель: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
