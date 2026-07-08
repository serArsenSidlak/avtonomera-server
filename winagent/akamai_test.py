"""Akamai bypass tester — run the opendata.hsc.gov.ua scraper flow ONCE from this server
and print a clear verdict on whether Akamai let us through.

Goal: try the plate scraper from different servers/regions/providers to see if any datacenter
IP passes Akamai's bot challenge (residential IP is normally required). This does a single
region attempt and reports PASSED / BLOCKED — nothing is sent anywhere.

Usage (fresh Ubuntu VM):
    bash akamai_test_setup.sh          # one-time: installs python, xvfb, playwright, chromium
    xvfb-run -a python3 akamai_test.py "Львівська"          # test one region
    xvfb-run -a python3 akamai_test.py "Львівська" http://user:pass@host:port   # via proxy

Reads nothing, writes nothing to the DB. Exit code 0 = passed, 1 = blocked/failed.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import sys
import urllib.request

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.expanduser("~/ms-playwright"))

from playwright.async_api import async_playwright

PAGE_URL = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HUMAN_STEP = 1.6


def _my_ip() -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.read().decode().strip()
        except Exception:  # noqa: BLE001
            continue
    return "?"


def _proxy_arg(proxy: str):
    if not proxy:
        return None
    m = re.match(r"^(?:(?P<scheme>\w+)://)?(?:(?P<user>[^:@]+):(?P<pw>[^@]+)@)?(?P<host>[^:@]+):(?P<port>\d+)$", proxy)
    if not m:
        return None
    d = m.groupdict()
    server = f"{d['scheme'] or 'http'}://{d['host']}:{d['port']}"
    arg = {"server": server}
    if d["user"]:
        arg["username"], arg["password"] = d["user"], d["pw"]
    return arg


async def _human(page, secs: int) -> None:
    """Synthetic mouse moves + wheel — what satisfies Akamai's sec-cpt behavioural challenge."""
    for i in range(secs):
        await page.mouse.move(random.randint(50, 1200), random.randint(50, 650), steps=random.randint(5, 14))
        if i % 2 == 0:
            try:
                await page.mouse.wheel(0, random.randint(-200, 200))
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(_HUMAN_STEP)


async def run(region: str, proxy: str) -> bool:
    print(f"→ Тестую з IP {_my_ip()}  регіон «{region}»" + (f"  через проксі {proxy.split('@')[-1]}" if proxy else ""),
          flush=True)
    launch = {"headless": False,
              "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--start-maximized"]}
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
            resp = await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else "?"
            print(f"   перший запит: HTTP {status}", flush=True)
            if status == 403:
                print("❌ BLOCKED — 403 одразу на вході (Akamai не пустив цей IP).", flush=True)
                return False
            # Try to pass the behavioural challenge and get the form selects to appear.
            selects = 0
            for _ in range(10):
                await _human(page, 6)
                selects = await page.evaluate("()=>document.querySelectorAll('select').length")
                if selects > 0:
                    break
                await asyncio.sleep(4)
            if selects == 0:
                print("❌ BLOCKED — форма не завантажилась (челендж не пройдено).", flush=True)
                return False
            print(f"   форма зʼявилась ({selects} select) — челендж пройдено ✅", flush=True)
            # Resolve region option value.
            opts = await page.evaluate(
                "()=>Array.from(document.querySelector('#region').options).map(o=>[o.value,o.textContent.trim()])")
            rv = None
            nn = re.sub(r"[^a-zа-яіїєґ0-9]", "", region.lower().replace("область", ""))
            for v, l in opts:
                ll = re.sub(r"[^a-zа-яіїєґ0-9]", "", l.lower().replace("область", ""))
                if v and v not in ("0", "-1") and ll and (nn == ll or nn in ll or ll in nn):
                    rv, region = v, l
                    break
            if not rv:
                print(f"⚠️  Регіон «{region}» не знайдено у списку — але Akamai ПРОЙДЕНО. Спробуй іншу назву.", flush=True)
                return True
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                await page.wait_for_selector("#region", timeout=15000)
            except Exception:  # noqa: BLE001
                for _ in range(8):
                    await _human(page, 6)
                    if await page.evaluate("()=>document.querySelectorAll('select').length") > 0:
                        break
                    await asyncio.sleep(4)
                await page.wait_for_selector("#region", timeout=20000)
            await page.select_option("#region", rv)
            await asyncio.sleep(random.uniform(0.9, 2.0))
            for sel in ("a.close_link", "text=Залишитись на основному сайті", "button.close"):
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    try:
                        await loc.click(timeout=2500)
                        break
                    except Exception:  # noqa: BLE001
                        pass
            await page.select_option("#tsc", "Весь регіон")
            await page.select_option("#type_venichle", "all")
            await asyncio.sleep(random.uniform(0.9, 2.0))
            async with page.expect_response(
                    lambda r: r.request.method == "POST" and "check-leisure-license-plates" in r.url,
                    timeout=45000) as ri:
                await page.locator("input[type=submit]").last.click(timeout=15000, no_wait_after=True)
            body = await (await ri.value).text()
            if "Номерний" not in body:
                print("❌ BLOCKED — сабміт пройшов, але таблиці немає (тихий блок).", flush=True)
                return False
            n = body.count("<tr")
            print(f"✅ PASSED — отримано дані ({region}): ~{max(0, n - 1)} рядків. Цей сервер ПРОХОДИТЬ Akamai! 🎉",
                  flush=True)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"❌ FAILED — {exc!r}", flush=True)
            return False
        finally:
            await browser.close()


if __name__ == "__main__":
    reg = sys.argv[1] if len(sys.argv) > 1 else "Львівська"
    prx = sys.argv[2] if len(sys.argv) > 2 else ""
    ok = asyncio.run(run(reg, prx))
    sys.exit(0 if ok else 1)
