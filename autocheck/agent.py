"""Автономера — AutoCheck-агент для Windows (перевірка авто по реєстру МВС).

Самодостатня програма (як парсер-агент). Відкриває локальну панель у браузері. Звідти можна:
  • завантажити всі архіви МВС (data.gov.ua) у локальну базу SQLite на цьому ПК;
  • миттєво шукати по номеру/VIN (марка, модель, рік, історія реєстрацій);
  • підключити базу до Telegram-бота через тунель Cloudflare (без проброса портів).

Запуск (Windows): просто запусти .exe → панель відкриється на http://127.0.0.1:8741
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autocheck_config import SECRET, SERVER_URL

PORT = 8741
CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

# Прямі ресурси набору 0ffd8b75-0628-48cc-952a-9302f9799ec0 (рік -> URL .zip).
RESOURCES: dict = {
    2013: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/86a9548b-8323-4fa2-972e-0692edf6959f/download/tz_opendata_z01012013_po31122013.zip",
    2014: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/80a115ae-61df-4a13-8771-36c2826268df/download/tz_opendata_z01012014_po31122014.zip",
    2015: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/09c606dc-d740-40db-96f0-e679eeca6ace/download/tz_opendata_z01012015_po31122015.zip",
    2016: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/7bdc2a1b-5399-4ab0-97e0-633e68837b04/download/tz_opendata_z01012016_po31122016.zip",
    2017: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/9ce32352-bd11-4324-a2b4-5addbd228b1b/download/tz_opendata_z01012017_po31122017.zip",
    2018: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/01323740-88df-46c2-b06e-fbb58c89fe17/download/tz_opendata_z01012018_po01012019.zip",
    2019: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/7a58e8f7-9323-47d4-a21d-19486e014eb4/download/tz_opendata_z01012019_po01012020.zip",
    2020: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/ebeb92fe-424c-41d1-aacf-288e91049dc9/download/tz_opendata_z01012020_po01012021.zip",
    2021: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/c5cb530d-0533-40be-b9ad-f03e06c94b10/download/tz_opendata_z01012021_po01012022.zip",
    2023: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/c3a12388-55c2-4546-8b71-b4b7ff0d8b16/download/reestrtz2023.zip",
    2024: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/c3ffecc4-bb5c-4102-b761-6dcfeb60b4fe/download/reestrtz2024.zip",
    2025: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/b7e72d22-55f5-4545-87dc-94e6c8ee03ef/download/reestrtz2025.zip",
    2026: "https://data.gov.ua/dataset/0ffd8b75-0628-48cc-952a-9302f9799ec0/resource/3f13166f-090b-499e-8e23-e9851c5a5f67/download/reestrtz2026.zip",
}

_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                          "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})
COLUMNS = ["vin", "plate", "brand", "model", "make_year", "color", "kind", "body", "purpose",
           "fuel", "capacity", "own_weight", "total_weight", "d_reg", "oper_code", "oper_name",
           "dep_code", "dep", "reg_addr_koatuu", "person", "src_year"]


def _appdir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AvtonomeraAutoCheck")
    os.makedirs(d, exist_ok=True)
    return d


DB_PATH = os.path.join(_appdir(), "autocheck.db")
CF_PATH = os.path.join(_appdir(), "cloudflared.exe")

_lock = threading.Lock()
STATE = {
    "years": {y: {"status": "—", "rows": 0} for y in sorted(RESOURCES)},
    "db_rows": 0, "loading": False, "progress": "", "tunnel_url": "", "registered": False,
}
_cf_proc = None


def _log(msg: str) -> None:
    with _lock:
        STATE["progress"] = msg
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _norm_plate(raw: str):
    p = re.sub(r"[\s\-]", "", (raw or "")).strip().upper().translate(_LAT2CYR)
    return p or None


def _int(v: str):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        return None


def _iso(v: str):
    v = (v or "").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return None


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS vehicle_ops (vin TEXT, plate TEXT, brand TEXT, model TEXT, "
        "make_year INTEGER, color TEXT, kind TEXT, body TEXT, purpose TEXT, fuel TEXT, "
        "capacity INTEGER, own_weight INTEGER, total_weight INTEGER, d_reg TEXT, oper_code INTEGER, "
        "oper_name TEXT, dep_code INTEGER, dep TEXT, reg_addr_koatuu TEXT, person TEXT, src_year INTEGER)")
    con.commit()


def _db_count() -> int:
    try:
        con = _connect()
        try:
            return con.execute("SELECT count(*) FROM vehicle_ops").fetchone()[0]
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return 0


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "avtonomera-autocheck/1.0"})
    with urllib.request.urlopen(req, timeout=900) as r, open(dest, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)


def _rows(csv_path: str, year: int):
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter=";"):
            yield (
                (row.get("VIN") or "").strip() or None,
                _norm_plate(row.get("N_REG_NEW")),
                (row.get("BRAND") or "").strip() or None,
                (row.get("MODEL") or "").strip() or None,
                _int(row.get("MAKE_YEAR")),
                (row.get("COLOR") or "").strip() or None,
                (row.get("KIND") or "").strip() or None,
                (row.get("BODY") or "").strip() or None,
                (row.get("PURPOSE") or "").strip() or None,
                (row.get("FUEL") or "").strip() or None,
                _int(row.get("CAPACITY")),
                _int(row.get("OWN_WEIGHT")),
                _int(row.get("TOTAL_WEIGHT")),
                _iso(row.get("D_REG")),
                _int(row.get("OPER_CODE")),
                (row.get("OPER_NAME") or "").strip() or None,
                _int(row.get("DEP_CODE")),
                (row.get("DEP") or "").strip() or None,
                (row.get("REG_ADDR_KOATUU") or "").strip() or None,
                (row.get("PERSON") or "").strip() or None,
                year,
            )


def _load_all() -> None:
    """Full reload: rebuild the SQLite DB from all yearly MVS archives, then index."""
    with _lock:
        if STATE["loading"]:
            return
        STATE["loading"] = True
    ins = f"INSERT INTO vehicle_ops ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"
    try:
        con = _connect()
        con.execute("DROP TABLE IF EXISTS vehicle_ops")
        con.execute("DROP INDEX IF EXISTS ix_plate")
        con.execute("DROP INDEX IF EXISTS ix_vin")
        _ensure_schema(con)
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        for y in sorted(RESOURCES):
            url = RESOURCES[y]
            _log(f"{y}: завантажую…")
            with _lock:
                STATE["years"][y] = {"status": "завантаження", "rows": 0}
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    zp = os.path.join(tmp, f"{y}.zip")
                    _download(url, zp)
                    with zipfile.ZipFile(zp) as zf:
                        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
                        zf.extractall(tmp)
                    yr_rows = 0
                    for name in names:
                        _log(f"{y}: заливаю {os.path.basename(name)}…")
                        batch = []
                        for rec in _rows(os.path.join(tmp, name), y):
                            batch.append(rec)
                            if len(batch) >= 20000:
                                con.executemany(ins, batch)
                                yr_rows += len(batch)
                                batch = []
                                with _lock:
                                    STATE["years"][y] = {"status": "заливка", "rows": yr_rows}
                        if batch:
                            con.executemany(ins, batch)
                            yr_rows += len(batch)
                    con.commit()
                with _lock:
                    STATE["years"][y] = {"status": "ok", "rows": yr_rows}
                _log(f"{y}: готово, {yr_rows} рядків ✅")
            except Exception as exc:  # noqa: BLE001
                with _lock:
                    STATE["years"][y] = {"status": "помилка", "rows": 0}
                _log(f"{y}: помилка ({exc}) ❌")
        _log("Будую індекси (номер, VIN)…")
        con.execute("CREATE INDEX IF NOT EXISTS ix_plate ON vehicle_ops(plate)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_vin ON vehicle_ops(vin)")
        con.commit()
        con.close()
        with _lock:
            STATE["db_rows"] = _db_count()
        _log(f"Готово. Усього в базі: {STATE['db_rows']} рядків.")
    except Exception as exc:  # noqa: BLE001
        _log(f"Помилка завантаження: {exc}")
    finally:
        with _lock:
            STATE["loading"] = False


def _dumps_dir() -> str:
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _iso2(v):
    v = (v or "").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", v)
    if m:
        y = m.group(3)
        y = "20" + y if len(y) == 2 else y
        return f"{y}-{m.group(2)}-{m.group(1)}"
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return None


_OPER_COMBINED = "CD.OPER_CODE||'-'||CD.OPERAS"


def _record_flex(row, src_year):
    """Один рядок дампу → кортеж COLUMNS. Підтримує старий і новий (2026) формати."""
    opn = row.get("OPER_NAME")
    opc = _int(row.get("OPER_CODE"))
    comb = row.get(_OPER_COMBINED) or ""
    if not opn and comb:
        parts = comb.split(" - ", 1)
        if opc is None:
            opc = _int(parts[0])
        opn = parts[1] if len(parts) > 1 else comb
    plate = _norm_plate(row.get("N_REG_NEW")) if row.get("N_REG_NEW") else None
    return ((row.get("VIN") or "").strip() or None, plate,
            (row.get("BRAND") or "").strip() or None, (row.get("MODEL") or "").strip() or None,
            _int(row.get("MAKE_YEAR")), (row.get("COLOR") or "").strip() or None,
            (row.get("KIND") or "").strip() or None, (row.get("BODY") or "").strip() or None,
            (row.get("PURPOSE") or "").strip() or None, (row.get("FUEL") or "").strip() or None,
            _int(row.get("CAPACITY")), _int(row.get("OWN_WEIGHT")), _int(row.get("TOTAL_WEIGHT")),
            _iso2(row.get("D_REG")), opc, opn, _int(row.get("DEP_CODE")),
            (row.get("DEP") or "").strip() or None, (row.get("REG_ADDR_KOATUU") or "").strip() or None,
            (row.get("PERSON") or "").strip() or None, src_year)


def _find_local_dumps():
    d = _dumps_dir()
    files = []
    for root in (d, os.path.join(d, "dumps")):
        if os.path.isdir(root):
            for f in os.listdir(root):
                p = os.path.join(root, f)
                if os.path.isfile(p) and (f.lower().endswith(".zip") or f.lower().endswith(".csv")):
                    files.append(p)
    return sorted(set(files))


def _build_local() -> None:
    """Побудувати базу з дампів, що лежать поряд із .exe (без завантаження з мережі)."""
    import csv as _csv
    import io as _io
    import zipfile as _zip

    with _lock:
        if STATE["loading"]:
            return
        STATE["loading"] = True
    try:
        dumps = _find_local_dumps()
        if not dumps:
            _log("Дампів поряд не знайдено. Поклади reestrTZ*.zip у папку з цим файлом.")
            return
        ins = f"INSERT INTO vehicle_ops ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"
        con = _connect()
        con.execute("DROP TABLE IF EXISTS vehicle_ops")
        _ensure_schema(con)
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        total = 0
        for path in dumps:
            ym = re.search(r"(20\d{2})", os.path.basename(path))
            sy = int(ym.group(1)) if ym else None
            _log(f"Будую з {os.path.basename(path)}…")
            try:
                streams = []
                if path.lower().endswith(".zip"):
                    zf = _zip.ZipFile(path)
                    for n in zf.namelist():
                        if n.lower().endswith(".csv"):
                            streams.append(_io.TextIOWrapper(zf.open(n), encoding="utf-8", errors="replace", newline=""))
                else:
                    streams.append(open(path, encoding="utf-8", errors="replace", newline=""))
                for st in streams:
                    batch = []
                    for row in _csv.DictReader(st, delimiter=";"):
                        batch.append(_record_flex(row, sy))
                        if len(batch) >= 20000:
                            con.executemany(ins, batch); total += len(batch); batch = []
                            _log(f"{os.path.basename(path)}: {total} рядків…")
                    if batch:
                        con.executemany(ins, batch); total += len(batch)
                con.commit()
            except Exception as exc:  # noqa: BLE001
                _log(f"{os.path.basename(path)}: помилка ({exc})")
        _log("Будую індекси…")
        con.execute("CREATE INDEX IF NOT EXISTS ix_plate ON vehicle_ops(plate)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_vin ON vehicle_ops(vin)")
        con.commit()
        con.close()
        with _lock:
            STATE["db_rows"] = _db_count()
        _log(f"База готова: {STATE['db_rows']} рядків.")
    except Exception as exc:  # noqa: BLE001
        _log(f"Помилка побудови: {exc}")
    finally:
        with _lock:
            STATE["loading"] = False


def _lookup(plate=None, vin=None) -> dict:
    con = _connect()
    try:
        if plate:
            rows = con.execute("SELECT * FROM vehicle_ops WHERE plate=? ORDER BY d_reg",
                               (_norm_plate(plate),)).fetchall()
        elif vin:
            rows = con.execute("SELECT * FROM vehicle_ops WHERE vin=? ORDER BY d_reg",
                               ((vin or "").strip().upper(),)).fetchall()
        else:
            return {"found": False}
    finally:
        con.close()
    if not rows:
        return {"found": False}
    last = rows[-1]
    veh = {k: last[k] for k in ("vin", "plate", "brand", "model", "make_year", "color",
                                "kind", "body", "fuel", "capacity")}
    history = [{"d_reg": r["d_reg"], "oper_name": r["oper_name"], "dep": r["dep"],
               "plate": r["plate"]} for r in rows]
    return {"found": True, "vehicle": veh, "history": history}


def _register(url: str) -> bool:
    body = json.dumps({"secret": SECRET, "url": url}).encode("utf-8")
    req = urllib.request.Request(SERVER_URL.rstrip("/") + "/autocheck/register", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except Exception as exc:  # noqa: BLE001
        _log(f"Реєстрація на сервері не вдалась: {exc}")
        return False


def _start_tunnel() -> None:
    """Download cloudflared if needed, open a quick tunnel, register its URL with the server."""
    global _cf_proc
    if not os.path.exists(CF_PATH):
        _log("Завантажую cloudflared…")
        try:
            _download(CF_URL, CF_PATH)
        except Exception as exc:  # noqa: BLE001
            _log(f"Не вдалося завантажити cloudflared: {exc}")
            return
    _log("Піднімаю тунель…")
    _cf_proc = subprocess.Popen(
        [CF_PATH, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for _ in range(120):
        line = _cf_proc.stdout.readline()
        if not line:
            if _cf_proc.poll() is not None:
                break
            continue
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m and not m.group(0).startswith("https://api."):  # пропускаємо службовий api.trycloudflare.com
            url = m.group(0)
            with _lock:
                STATE["tunnel_url"] = url
            ok = _register(url)
            with _lock:
                STATE["registered"] = ok
            _log(f"Тунель активний: {url} {'(підключено до бота ✅)' if ok else ''}")
            break
    # keep draining output so the pipe doesn't block
    threading.Thread(target=lambda: [l for l in iter(_cf_proc.stdout.readline, "")], daemon=True).start()


PANEL = """<!doctype html><html lang=uk><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>AutoCheck — агент</title>
<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#eef1f6;margin:0;padding:18px}
h2{margin:0 0 12px}button{background:#3b82f6;color:#fff;border:0;border-radius:10px;padding:10px 15px;font-weight:700;cursor:pointer;margin:3px}
button.g{background:#16a34a}.bar{background:#161a22;border:1px solid #232834;border-radius:12px;padding:14px;margin-bottom:14px}
table{width:100%;border-collapse:collapse}td,th{padding:6px 8px;border-bottom:1px solid #232834;text-align:left;font-size:14px}
.muted{color:#9aa4b2}#prog{color:#7dd3fc;min-height:20px;margin-top:8px}code{background:#1b1f27;padding:2px 6px;border-radius:6px}
input{background:#1b1f27;color:#eef1f6;border:1px solid #333;border-radius:8px;padding:8px;width:170px}</style>
</head><body>
<h2>🚗 AutoCheck — агент перевірки авто</h2>
<div class=bar>
<button onclick="j('/api/build','POST')">🛠 Побудувати базу з дампів (поряд)</button>
<button onclick="j('/api/load','POST')">⬇️ Завантажити з мережі</button>
<button class=g onclick="j('/api/tunnel','POST')">🔗 Підключити до бота</button>
<div id=prog></div>
<div id=stat class=muted style="margin-top:8px"></div>
</div>
<div class=bar>
<b>Перевірити вручну:</b><br>
<input id=q placeholder="номер або VIN"><button onclick="look()">🔍 Пошук</button>
<pre id=res style="white-space:pre-wrap;font-size:13px"></pre>
</div>
<table id=tbl><thead><tr><th>Рік</th><th>Статус</th><th>Рядків</th></tr></thead><tbody></tbody></table>
<script>
async function j(u,m){const r=await fetch(u,{method:m||'GET'});return r.json();}
async function look(){const v=document.getElementById('q').value.trim();if(!v)return;
 const isVin=/[0-9]/.test(v)&&v.length>=10&&/[A-Z]/i.test(v)&&!/^[А-Яа-яІЇЄҐ]/.test(v);
 const r=await j('/lookup?'+(isVin?'vin=':'plate=')+encodeURIComponent(v));
 document.getElementById('res').textContent=JSON.stringify(r,null,2);}
async function tick(){const s=await j('/api/state');
 document.getElementById('prog').textContent=s.progress||'';
 document.getElementById('stat').innerHTML='База: <b>'+(s.db_rows||0)+'</b> рядків. '+
  (s.tunnel_url?('Тунель: <code>'+s.tunnel_url+'</code> '+(s.registered?'✅ підключено':'')):'Тунель не запущено.');
 const tb=document.querySelector('#tbl tbody');tb.innerHTML='';
 for(const y of Object.keys(s.years)){const r=s.years[y];
  tb.innerHTML+='<tr><td>'+y+'</td><td>'+r.status+'</td><td>'+(r.rows||0)+'</td></tr>';}}
setInterval(tick,1500);tick();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, html=False):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        data = obj if html else json.dumps(obj, ensure_ascii=False)
        self.wfile.write(data.encode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(PANEL, html=True)
        elif self.path == "/api/state":
            with _lock:
                self._send({"years": STATE["years"], "db_rows": STATE["db_rows"],
                            "loading": STATE["loading"], "progress": STATE["progress"],
                            "tunnel_url": STATE["tunnel_url"], "registered": STATE["registered"]})
        elif self.path.startswith("/lookup"):
            # secret required only for remote (bot) calls; local panel calls without it are allowed.
            from urllib.parse import urlparse, parse_qs

            q = parse_qs(urlparse(self.path).query)
            remote = self.client_address[0] not in ("127.0.0.1", "::1")
            if remote and self.headers.get("x-secret") != SECRET:
                self._send({"error": "forbidden"}, 403)
                return
            self._send(_lookup(plate=(q.get("plate") or [None])[0], vin=(q.get("vin") or [None])[0]))
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/load":
            threading.Thread(target=_load_all, daemon=True).start()
            self._send({"started": True})
        elif self.path == "/api/build":
            threading.Thread(target=_build_local, daemon=True).start()
            self._send({"started": True})
        elif self.path == "/api/tunnel":
            threading.Thread(target=_start_tunnel, daemon=True).start()
            self._send({"started": True})
        else:
            self._send({"error": "not found"}, 404)


def _autostart() -> None:
    """Авто: якщо бази нема, а дампи поряд — побудувати; потім авто-підключення до бота."""
    rows = _db_count()
    if rows == 0 and _find_local_dumps():
        _log("Знайшов дампи поряд — будую базу…")
        _build_local()
    if _db_count() > 0:
        _log("Підключаю до бота…")
        _start_tunnel()
    else:
        _log("Бази немає. Поклади дампи поряд і натисни «🛠 Побудувати», або «⬇️ Завантажити з мережі».")


def main() -> None:
    with _lock:
        STATE["db_rows"] = _db_count()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Панель: {url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_autostart, daemon=True).start()  # авто-побудова/підключення
    srv.serve_forever()


if __name__ == "__main__":
    main()
