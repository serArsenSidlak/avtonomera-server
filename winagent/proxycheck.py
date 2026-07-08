"""Проксі-чекер для opendata.hsc.gov.ua — швидко (без браузера) перевіряє, чи ріже Akamai
IP «на вході» (403) на найпершому запиті, ще до JS-челенджу.

Логіка: перший HTTPS-запит до порталу через кожен проксі. Akamai віддає 403 датацентровим/
спаленим IP ДО того, як завантажиться сторінка з поведінковим челенджем. Тому:
  • 403  → BLOCKED «на вході» (IP порізаний одразу — миша/затримки вже не врятують);
  • 200  → пройшов вхідний бар'єр → КАНДИДАТ на повний браузерний тест (akamai_test.py);
  • 000/таймаут/інше → проксі мертвий/недоступний.

Підтримує http/https/socks4/socks4a/socks5/socks5h і формати:
  ip:port | ip:port:user:pass | user:pass@ip:port | scheme://... (з логіном/без)
MTProto-рядки (server=…&secret=…) автоматично пропускаються (їх не можна юзати для веба).

Використання:
  python3 proxycheck.py proxies.txt              # список із файлу (по рядку на проксі)
  pbpaste | python3 proxycheck.py                # зі stdin (напр. вставлений список)
  python3 proxycheck.py proxies.txt --timeout 12 --workers 60
  python3 proxycheck.py proxies.txt --geo        # + показати країну/exit-IP кандидатів

Вихід: рядок на кожен проксі + підсумок. Наприкінці — список КАНДИДАТІВ (200), якщо є.
Потрібен лише `curl` (є в системі). Нічого нікуди не зберігає.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

TARGET = "https://opendata.hsc.gov.ua/check-leisure-license-plates/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_SCHEMES = ("http://", "https://", "socks4://", "socks4a://", "socks5://", "socks5h://")
_IPPORT = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9.-]+\.[a-z]{2,}):(\d{2,5})", re.I)


def to_proxy_url(line: str) -> str | None:
    """Normalise a raw line into a curl -x proxy URL, or None if it isn't a usable web proxy."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if "secret=" in s or "mtproto" in s.lower() or s.lower().startswith("tg://"):
        return None  # MTProto / Telegram proxy — not usable for web
    if s.startswith(_SCHEMES):
        return s
    # user:pass@host:port  (no scheme)
    if "@" in s and _IPPORT.search(s.split("@", 1)[1]):
        return "http://" + s
    parts = s.split(":")
    if len(parts) == 2 and _IPPORT.match(s):
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:  # host:port:user:pass
        host, port, user, pw = parts
        return f"http://{user}:{pw}@{host}:{port}"
    m = _IPPORT.search(s)  # last resort: pull an ip:port out of noisy text
    return f"http://{m.group(1)}:{m.group(2)}" if m else None


def _curl_code(proxy: str, url: str, timeout: int) -> str:
    try:
        r = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", str(timeout),
             "-x", proxy, "-A", UA, url],
            capture_output=True, text=True, timeout=timeout + 5)
        return (r.stdout or "000").strip() or "000"
    except Exception:  # noqa: BLE001
        return "000"


def _geo(proxy: str, timeout: int) -> str:
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(timeout), "-x", proxy,
                            "https://ipinfo.io/json"], capture_output=True, text=True, timeout=timeout + 5)
        import json
        d = json.loads(r.stdout)
        return f"{d.get('ip','?')} {d.get('country','?')}"
    except Exception:  # noqa: BLE001
        return "?"


def check(proxy: str, url: str, timeout: int, geo: bool) -> dict:
    code = _curl_code(proxy, url, timeout)
    if code == "403":
        verdict = "❌ BLOCKED (403 на вході)"
    elif code == "200":
        verdict = "✅ PASSED вхід → КАНДИДАТ"
    elif code in ("000", ""):
        verdict = "· мертвий/таймаут"
    else:
        verdict = f"? HTTP {code}"
    info = _geo(proxy, timeout) if (geo and code not in ("000", "403")) else ""
    return {"proxy": proxy, "code": code, "verdict": verdict, "info": info}


def main() -> int:
    ap = argparse.ArgumentParser(description="Akamai entry-gate proxy checker for opendata.hsc.gov.ua")
    ap.add_argument("file", nargs="?", help="файл зі списком проксі (без нього — читає stdin)")
    ap.add_argument("--timeout", type=int, default=12, help="таймаут на проксі, сек (12)")
    ap.add_argument("--workers", type=int, default=50, help="паралельних перевірок (50)")
    ap.add_argument("--url", default=TARGET, help="цільовий URL (за замовч. портал ГСЦ)")
    ap.add_argument("--geo", action="store_true", help="показати країну/IP кандидатів")
    a = ap.parse_args()

    raw = open(a.file, encoding="utf-8", errors="ignore").read() if a.file else sys.stdin.read()
    proxies, skipped = [], 0
    seen = set()
    for ln in raw.splitlines():
        p = to_proxy_url(ln)
        if p is None:
            if ln.strip() and ("secret=" in ln or "mtproto" in ln.lower()):
                skipped += 1
            continue
        if p not in seen:
            seen.add(p)
            proxies.append(p)

    print(f"Перевіряю {len(proxies)} проксі проти {a.url}"
          + (f"  (пропущено MTProto: {skipped})" if skipped else ""), flush=True)
    if not proxies:
        print("Немає придатних веб-проксі у вводі (потрібні http/socks, не MTProto).")
        return 1

    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(lambda p: check(p, a.url, a.timeout, a.geo), proxies):
            tail = f"  [{r['info']}]" if r["info"] else ""
            print(f"{r['verdict']:<28} {r['proxy']}{tail}", flush=True)
            results.append(r)

    passed = [r for r in results if r["code"] == "200"]
    blocked = sum(1 for r in results if r["code"] == "403")
    dead = sum(1 for r in results if r["code"] in ("000", ""))
    print("\n" + "=" * 50)
    print(f"РАЗОМ: {len(results)}  |  ✅ пройшли вхід: {len(passed)}  |  "
          f"❌ 403 на вході: {blocked}  |  · мертві: {dead}  |  ? інше: {len(results)-len(passed)-blocked-dead}")
    if passed:
        print("\n🎯 КАНДИДАТИ (пройшли вхідний бар'єр — тестуй повним браузерним akamai_test.py):")
        for r in passed:
            print(f"   {r['proxy']}" + (f"  [{r['info']}]" if r["info"] else ""))
    else:
        print("\nЖоден не пройшов вхідний бар'єр — усі порізані на вході або мертві "
              "(типово для datacenter/безкоштовних; треба residential/mobile).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
