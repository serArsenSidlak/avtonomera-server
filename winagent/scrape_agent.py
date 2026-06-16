"""Автономера — Windows scraping agent (residential IP) → pushes data to the server.

Runs in the background on a "friendly" PC. Scrapes the HSC portal with a real (headed,
off-screen) Chromium to pass Akamai, and POSTs results to the server's /ingest endpoint.

Schedule (per product spec):
  • every 3h — all regions, vehicle types «Легковий, вантажний» then «Електромобіль» (priority)
  • every 6h — all regions, all other vehicle types

Self-contained (no `local` package): safe to bundle into a single Windows .exe via PyInstaller.
"""
from __future__ import annotations

import os

# Pin Playwright's browser dir to a fixed, writable user path so that the browser DOWNLOAD
# (ensure_browser) and the browser LAUNCH use the SAME location. Without this, a PyInstaller
# one-file build downloads Chromium to %LOCALAPPDATA%\ms-playwright but tries to launch it from
# the read-only bundle temp dir → "Executable doesn't exist". Must be set BEFORE importing playwright.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ms-playwright"
)

import asyncio
import json
import random
import re
import sys
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from agent_config import INGEST_SECRET, SERVER_URL

PAGE_URL = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
FAST_INTERVAL = 3 * 3600
SLOW_INTERVAL = 6 * 3600

_LATIN_TO_CYRILLIC = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
    "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х",
})
_PRICE_RE = re.compile(r"[\d.,]+")


def log(msg: str) -> None:
    """Timestamped stdout log."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def normalize_plate(raw: str) -> str:
    """Uppercase, strip separators, transliterate Latin→Cyrillic."""
    return re.sub(r"[\s\-]", "", raw or "").strip().upper().translate(_LATIN_TO_CYRILLIC)


def _price(text: str) -> Optional[float]:
    m = _PRICE_RE.search((text or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _parse_table(html: str, region: str, vehicle_type: Optional[str]) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        head = t.find("tr")
        if head and "Номерний" in head.get_text():
            table = t
            break
    if table is None:
        return []
    rows: List[Dict[str, Any]] = []
    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        plate = tds[0].get_text(strip=True)
        if not plate or "Номерний" in plate:
            continue
        rows.append({
            "plate_number": normalize_plate(plate),
            "price": _price(tds[1].get_text(strip=True)),
            "tsc": tds[2].get_text(strip=True) or "—",
            "region": region,
            "vehicle_type": vehicle_type,
        })
    return rows


async def _human(page, secs: int) -> None:
    for i in range(secs):
        await page.mouse.move(random.randint(50, 1300), random.randint(50, 700),
                              steps=random.randint(3, 10))
        if i % 3 == 0:
            await page.mouse.wheel(0, random.randint(-150, 150))
        await asyncio.sleep(1)


async def _options(page, css_id: str) -> List[Tuple[str, str]]:
    raw = await page.evaluate(
        "(id)=>{const s=document.querySelector(id);return s?Array.from(s.options)"
        ".map(o=>[o.value,o.textContent.trim()]):[]}",
        css_id,
    )
    return [(v, l) for v, l in raw if v and v not in ("0", "-1") and l]


async def scrape(type_filter: Callable[[str], bool]) -> Dict[str, Any]:
    """Scrape ALL regions for vehicle types where type_filter(label) is True."""
    out: List[Dict[str, Any]] = []
    ok_scopes: set = set()
    fail_scopes: List[Tuple[str, str]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # headed passes Akamai; window pushed off-screen below
            args=[
                "--disable-blink-features=AutomationControlled", "--no-sandbox",
                "--window-position=-2400,-2400", "--window-size=1366,768",
            ],
        )
        ctx = await browser.new_context(
            locale="uk-UA", timezone_id="Europe/Kyiv", user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['uk-UA','uk','en-US']});"
            "window.chrome=window.chrome||{runtime:{}};"
        )
        page = await ctx.new_page()
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            for _ in range(8):
                await _human(page, 4)
                if await page.evaluate("()=>document.querySelectorAll('select').length") > 0:
                    break
                await asyncio.sleep(3)
            else:
                raise RuntimeError("Akamai challenge not passed (no form)")
            regions = await _options(page, "#region")
            types = [(v, l) for v, l in await _options(page, "#type_venichle")
                     if v != "all" and type_filter(l)]
            # priority order: «Легковий…» first, then «Електромобіль», then the rest
            types.sort(key=lambda vl: (0 if "Легков" in vl[1] else 1 if "Електромобіль" in vl[1] else 2))
            log(f"regions={len(regions)} types={[l for _, l in types]}")
            for rv, rn in regions:
                for tv, tl in types:
                    try:
                        await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
                        try:
                            await page.wait_for_selector("#region", timeout=15000)
                        except Exception:
                            for _ in range(6):
                                await _human(page, 4)
                                if await page.evaluate("()=>document.querySelectorAll('select').length") > 0:
                                    break
                                await asyncio.sleep(3)
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
                        await page.select_option("#tsc", "Весь регіон")
                        await page.select_option("#type_venichle", tv)
                        await asyncio.sleep(random.uniform(0.4, 1.0))
                        async with page.expect_response(
                            lambda r: r.request.method == "POST" and "check-leisure-license-plates" in r.url,
                            timeout=45000,
                        ) as ri:
                            await page.locator("input[type=submit]").last.click(timeout=10000)
                        body = await (await ri.value).text()
                        if "Номерний" not in body:
                            raise RuntimeError("no results table (likely blocked)")
                        out.extend(_parse_table(body, rn, tl))
                        ok_scopes.add((rn, tl))
                        await asyncio.sleep(random.uniform(0.5, 1.2))
                    except Exception as exc:  # noqa: BLE001
                        fail_scopes.append((rn, tl))
                        log(f"{rn}/{tl} FAILED: {exc!r}")
                log(f"region done: {rn} · running total {len(out)}")
        finally:
            await browser.close()
    return {"rows": out, "ok_scopes": ok_scopes, "fail_scopes": fail_scopes}


def _push(rows: List[Dict[str, Any]], ok_scopes) -> None:
    """POST scraped rows to the server's /ingest endpoint."""
    payload = json.dumps({
        "secret": INGEST_SECRET,
        "rows": rows,
        "ok_scopes": [list(s) for s in ok_scopes],
    }).encode("utf-8")
    url = SERVER_URL.rstrip("/") + "/ingest"
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        log(f"server response: {r.read().decode('utf-8')}")


async def _run_group(name: str, type_filter: Callable[[str], bool]) -> None:
    log(f"=== {name} scan start ===")
    try:
        res = await scrape(type_filter)
        log(f"{name}: rows={len(res['rows'])} ok={len(res['ok_scopes'])} failed={len(res['fail_scopes'])}")
        if res["rows"] or res["ok_scopes"]:
            _push(res["rows"], res["ok_scopes"])
    except Exception as exc:  # noqa: BLE001
        log(f"{name}: FAILED {exc!r}")


def _is_fast(label: str) -> bool:
    return ("Легков" in label) or (label.strip() == "Електромобіль")


def ensure_browser() -> None:
    """Make sure Chromium is installed (downloads ~150 MB on first run only)."""
    try:
        log("Перевіряю/встановлюю браузер Chromium (перший раз — кілька хвилин)…")
        from playwright.__main__ import main as pw_main
        argv = sys.argv
        sys.argv = ["playwright", "install", "chromium"]
        try:
            pw_main()
        except SystemExit:
            pass
        finally:
            sys.argv = argv
        log("Браузер готовий.")
    except Exception as exc:  # noqa: BLE001
        log(f"ensure_browser warning: {exc!r}")


async def main() -> None:
    if not INGEST_SECRET:
        log("ERROR: INGEST_SECRET is empty (build misconfigured). Exiting.")
        sys.exit(2)
    ensure_browser()
    log(f"Агент запущено. Сервер: {SERVER_URL}. Розклад: швидкі 3 год / решта 6 год.")
    last_fast = last_slow = 0.0
    while True:
        now = time.monotonic()
        if now - last_fast >= FAST_INTERVAL or last_fast == 0:
            await _run_group("FAST (Легковий/вантажний + Електромобіль)", _is_fast)
            last_fast = time.monotonic()
        if now - last_slow >= SLOW_INTERVAL or last_slow == 0:
            await _run_group("SLOW (інші типи ТЗ)", lambda l: not _is_fast(l))
            last_slow = time.monotonic()
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
