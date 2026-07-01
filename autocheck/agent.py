"""Автономера — AutoCheck-агент для Windows (перевірка авто по реєстру МВС).

Самодостатня програма (як парсер-агент). Відкриває локальну панель у браузері. Звідти можна:
  • завантажити всі архіви МВС (data.gov.ua) у локальну базу SQLite на цьому ПК;
  • миттєво шукати по номеру/VIN (марка, модель, рік, історія реєстрацій);
  • підключити базу до Telegram-бота БЕЗ тунеля/портів — агент сам опитує сервер
    (лише вихідні HTTP-запити), бачить запит від бота, шукає в базі й повертає результат.

Запуск (Windows): просто запусти .exe → панель відкриється на http://127.0.0.1:8741
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
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
           "dep_code", "dep", "reg_addr_koatuu", "person", "src_year",
           "letters_start", "digits", "letters_end",  # розібраний номер → пошук по комбінації/серії
           "row_hash"]  # хеш операції (VIN+номер+дата+операція+ТСЦ) → дедуп/доповнення при імпорті


def _row_hash(vin, plate, d_reg, oper_name, dep):
    """Стабільний хеш операції — щоб не дублювати той самий запис при повторному заливі."""
    import hashlib
    key = "|".join(str(x if x is not None else "") for x in (vin, plate, d_reg, oper_name, dep))
    return hashlib.md5(key.encode("utf-8")).hexdigest()
_PLATE_RE = re.compile(r"^([А-ЯІЇЄҐ]{1,3})(\d{2,4})([А-ЯІЇЄҐ]{0,3})$")


def _plate_parts(plate):
    """Розібрати номер на (літери_початку, цифри, літери_кінця)."""
    if not plate:
        return (None, None, None)
    m = _PLATE_RE.match(plate)
    if not m:
        return (None, None, None)
    return (m.group(1) or None, m.group(2) or None, m.group(3) or None)


def _appdir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AvtonomeraAutoCheck")
    os.makedirs(d, exist_ok=True)
    return d


DB_PATH = os.path.join(_appdir(), "autocheck.db")  # default; overridden by saved config


def _base_dir() -> str:
    return os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))


def _cfg_file() -> str:
    return os.path.join(_base_dir(), "autocheck_paths.json")


def _default_paths() -> dict:
    b = _base_dir()
    return {"db_dir": _appdir(), "dumps_dir": b, "import_dir": os.path.join(b, "import")}


def _load_paths() -> dict:
    d = _default_paths()
    try:
        if os.path.exists(_cfg_file()):
            with open(_cfg_file(), encoding="utf-8") as f:
                d.update({k: v for k, v in json.load(f).items() if v})
    except Exception:  # noqa: BLE001
        pass
    return d


DUMPS_DIR = _default_paths()["dumps_dir"]
IMPORT_DIR = _default_paths()["import_dir"]


def _apply_paths(d: dict = None) -> dict:
    """Застосувати шляхи (папка бази / основних дампів / додаткових) до глобальних змінних."""
    global DB_PATH, DUMPS_DIR, IMPORT_DIR
    d = d or _load_paths()
    try:
        os.makedirs(d["db_dir"], exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    DB_PATH = os.path.join(d["db_dir"], "autocheck.db")
    DUMPS_DIR = d["dumps_dir"]
    IMPORT_DIR = d["import_dir"]
    return d


def _save_paths(new: dict) -> dict:
    d = _load_paths()
    d.update({k: v for k, v in (new or {}).items() if v})
    try:
        with open(_cfg_file(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    return _apply_paths(d)


_apply_paths()  # застосувати збережені шляхи на старті

_lock = threading.Lock()
STATE = {
    "years": {y: {"status": "—", "rows": 0} for y in sorted(RESOURCES)},
    "db_rows": 0, "loading": False, "progress": "", "polling": False, "connected": False,
    "answered": 0,
}


def _log(msg: str) -> None:
    with _lock:
        STATE["progress"] = msg
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _norm_plate(raw: str):
    p = re.sub(r"[\s\-]", "", (raw or "")).strip().upper().translate(_LAT2CYR)
    return p or None


def _zip_data_names(zf):
    """Файли даних у zip. Деякі архіви МВС мають зіпсовану назву (2019: «…ßsv» замість «.csv»),
    тож беремо за змістом назви; якщо нічого не збіглось — усі файли (він там лише один)."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    data = [n for n in names if n.lower().endswith(".csv")
            or "opendata" in n.lower() or "reestr" in n.lower() or "tz_" in n.lower()]
    return data or names


def _int(v: str):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        return None


def _iso(v: str):
    """dd.mm.yyyy АБО dd.mm.yy (2-значний рік у дампах 2024-2026) → ISO YYYY-MM-DD."""
    v = (v or "").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", v)
    if m:
        y = m.group(3)
        y = "20" + y if len(y) == 2 else y
        return f"{y}-{m.group(2)}-{m.group(1)}"
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
        "oper_name TEXT, dep_code INTEGER, dep TEXT, reg_addr_koatuu TEXT, person TEXT, src_year INTEGER, "
        "letters_start TEXT, digits TEXT, letters_end TEXT, row_hash TEXT)")
    con.commit()


def _reset_db_file() -> None:
    """Видалити фізичний файл БД (і журнали) перед чистим білдом — щоб пошкоджений
    файл («database disk image is malformed») не блокував побудову (DROP TABLE на битому
    файлі падає, а видалення файлу — ні)."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = DB_PATH + suffix
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:  # noqa: BLE001
            pass


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
            plate = _norm_plate(row.get("N_REG_NEW"))
            ls, dg, le = _plate_parts(plate)
            yield (
                (row.get("VIN") or "").strip() or None,
                plate,
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
                year, ls, dg, le,
                _row_hash((row.get("VIN") or "").strip() or None, plate, _iso(row.get("D_REG")),
                          (row.get("OPER_NAME") or "").strip() or None,
                          (row.get("DEP") or "").strip() or None),
            )


def _load_all() -> None:
    """Full reload: rebuild the SQLite DB from all yearly MVS archives, then index."""
    with _lock:
        if STATE["loading"]:
            return
        STATE["loading"] = True
    ins = f"INSERT INTO vehicle_ops ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"
    try:
        _reset_db_file()  # чистий старт (і від пошкодженого файлу теж)
        con = _connect()
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
                        names = _zip_data_names(zf)
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
        con.execute("CREATE INDEX IF NOT EXISTS ix_digits ON vehicle_ops(digits)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_le ON vehicle_ops(letters_end)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_hash ON vehicle_ops(row_hash)")
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
    return DUMPS_DIR  # налаштовується у панелі (папка основних дампів)


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
    ls, dg, le = _plate_parts(plate)
    return ((row.get("VIN") or "").strip() or None, plate,
            (row.get("BRAND") or "").strip() or None, (row.get("MODEL") or "").strip() or None,
            _int(row.get("MAKE_YEAR")), (row.get("COLOR") or "").strip() or None,
            (row.get("KIND") or "").strip() or None, (row.get("BODY") or "").strip() or None,
            (row.get("PURPOSE") or "").strip() or None, (row.get("FUEL") or "").strip() or None,
            _int(row.get("CAPACITY")), _int(row.get("OWN_WEIGHT")), _int(row.get("TOTAL_WEIGHT")),
            _iso2(row.get("D_REG")), opc, opn, _int(row.get("DEP_CODE")),
            (row.get("DEP") or "").strip() or None, (row.get("REG_ADDR_KOATUU") or "").strip() or None,
            (row.get("PERSON") or "").strip() or None, src_year, ls, dg, le,
            _row_hash((row.get("VIN") or "").strip() or None, plate, _iso2(row.get("D_REG")), opn,
                      (row.get("DEP") or "").strip() or None))


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
        _reset_db_file()  # чистий старт (і від пошкодженого файлу теж)
        con = _connect()
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
                    for n in _zip_data_names(zf):
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
        con.execute("CREATE INDEX IF NOT EXISTS ix_digits ON vehicle_ops(digits)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_le ON vehicle_ops(letters_end)")
        con.execute("CREATE INDEX IF NOT EXISTS ix_hash ON vehicle_ops(row_hash)")
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


# ── Universal import: залити БУДЬ-ЯКИЙ дамп; нові колонки додаються самі ──────────────────────
_CANON_MAP = {
    "VIN": "vin", "N_REG_NEW": "plate", "BRAND": "brand", "MODEL": "model", "MAKE_YEAR": "make_year",
    "COLOR": "color", "KIND": "kind", "BODY": "body", "PURPOSE": "purpose", "FUEL": "fuel",
    "CAPACITY": "capacity", "OWN_WEIGHT": "own_weight", "TOTAL_WEIGHT": "total_weight",
    "D_REG": "d_reg", "OPER_CODE": "oper_code", "OPER_NAME": "oper_name", "DEP_CODE": "dep_code",
    "DEP": "dep", "REG_ADDR_KOATUU": "reg_addr_koatuu", "PERSON": "person",
}


def _san_col(raw):
    """Безпечна назва sqlite-колонки з довільного заголовка дампа."""
    c = re.sub(r"[^a-z0-9_]", "_", (raw or "").strip().lower())
    return re.sub(r"_+", "_", c).strip("_") or "col"


def _table_cols(con):
    return {r[1] for r in con.execute("PRAGMA table_info(vehicle_ops)")}


def _import_csv(con, csv_path, src_year=None):
    """Залити один CSV. Відомі поля мапляться на канонічні; НЕВІДОМІ — стають новими колонками."""
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as fh:
        header = fh.readline()
    delim = ";" if header.count(";") >= header.count(",") else ","
    existing = _table_cols(con)
    added = []
    with open(csv_path, encoding="utf-8", errors="replace", newline="") as fh:
        r = csv.DictReader(fh, delimiter=delim)
        colmap = {}
        for h in (r.fieldnames or []):
            col = _CANON_MAP.get((h or "").strip().upper()) or _san_col(h)
            colmap[h] = col
            if col and col not in existing:
                con.execute(f'ALTER TABLE vehicle_ops ADD COLUMN "{col}" TEXT')
                existing.add(col)
                added.append(col)
        for c in ("letters_start", "digits", "letters_end", "src_year", "row_hash"):
            if c not in existing:
                con.execute(f'ALTER TABLE vehicle_ops ADD COLUMN "{c}" TEXT')
                existing.add(c)
        con.execute("CREATE INDEX IF NOT EXISTS ix_hash ON vehicle_ops(row_hash)")
        # Формат 2026: операція в об'єднаній колонці «код - назва» замість окремого OPER_NAME.
        comb_h = next((hh for hh in (r.fieldnames or []) if "OPERAS" in (hh or "").upper()), None)
        derive_oper = bool(comb_h) and "oper_name" not in colmap.values()
        if derive_oper:
            colmap.pop(comb_h, None)  # не зберігаємо об'єднану колонку сирою — розбираємо нижче
        base_cols = list(colmap.values()) + ["letters_start", "digits", "letters_end", "src_year", "row_hash"]
        if derive_oper:
            base_cols += ["oper_name", "oper_code"]
        cols = []
        for c in base_cols:
            if c not in cols:
                cols.append(c)
        ins = f'INSERT INTO vehicle_ops ({",".join(chr(34) + c + chr(34) for c in cols)}) ' \
              f'VALUES ({",".join("?" * len(cols))})'
        cur = con.cursor()
        added_rows = enriched = dup = 0
        for row in r:
            vals = {}
            for h, col in colmap.items():
                v = (row.get(h) or "").strip()
                if col == "plate":
                    v = _norm_plate(v)
                elif col == "vin":
                    v = v.upper() or None
                elif col == "d_reg":
                    v = _iso(v)
                vals[col] = v or None
            if derive_oper and not vals.get("oper_name"):
                comb = (row.get(comb_h) or "").strip()
                if comb:
                    m = re.match(r"\s*(\d+)\s*-\s*(.+)", comb)  # «код-назва» або «код - назва»
                    if m:
                        vals["oper_code"] = _int(m.group(1))
                        vals["oper_name"] = m.group(2).strip()
                    else:
                        vals["oper_name"] = comb
            ls, dg, le = _plate_parts(vals.get("plate")) if vals.get("plate") else (None, None, None)
            h = _row_hash(vals.get("vin"), vals.get("plate"), vals.get("d_reg"),
                          vals.get("oper_name"), vals.get("dep"))
            vals.update(letters_start=ls, digits=dg, letters_end=le, src_year=src_year, row_hash=h)
            ex = cur.execute("SELECT * FROM vehicle_ops WHERE row_hash=? LIMIT 1", (h,)).fetchone()
            if ex is None:  # нова операція → додаємо
                cur.execute(ins, tuple(vals.get(c) for c in cols))
                added_rows += 1
            else:  # така операція вже є → доповнюємо порожні поля (не затираємо заповнені)
                exd = dict(ex)
                upd = {c: vals[c] for c in cols
                       if c != "row_hash" and exd.get(c) in (None, "") and vals.get(c) not in (None, "")}
                if upd:
                    setc = ", ".join(f'"{c}"=?' for c in upd)
                    cur.execute(f"UPDATE vehicle_ops SET {setc} WHERE row_hash=?",
                                tuple(upd.values()) + (h,))
                    enriched += 1
                else:
                    dup += 1
    con.commit()
    return {"added": added_rows, "enriched": enriched, "dup": dup, "new_cols": added}


def _import_paths(files, source="Імпорт") -> None:
    """Залити список файлів (CSV/ZIP) у наявну базу з дедупом+доповненням.
    Спільне для «папки додаткових» і для завантаженого через панель файлу."""
    if not files:
        _log("Немає файлів для заливки.")
        return
    with _lock:
        if STATE["loading"]:
            _log("Зайнято (йде інша операція) — спробуй за мить.")
            return
        STATE["loading"] = True
    try:
        con = _connect()
        _ensure_schema(con)
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        agg = {"added": 0, "enriched": 0, "dup": 0}
        allcols = []

        def _apply(res, label):
            for k in ("added", "enriched", "dup"):
                agg[k] += res.get(k, 0)
            allcols.extend(res.get("new_cols") or [])
            nc = f" · нові поля: {', '.join(res['new_cols'])}" if res.get("new_cols") else ""
            _log(f"{label}: ➕{res['added']} 🔄{res['enriched']} ⏭{res['dup']}{nc}")

        for path in files:
            ym = re.search(r"(20\d{2})", os.path.basename(path))
            sy = int(ym.group(1)) if ym else None
            try:
                if path.lower().endswith(".zip"):
                    with tempfile.TemporaryDirectory() as tmp:
                        with zipfile.ZipFile(path) as zf:
                            names = _zip_data_names(zf)
                            zf.extractall(tmp)
                        for nm in names:
                            _apply(_import_csv(con, os.path.join(tmp, nm), sy), os.path.basename(path))
                else:
                    _apply(_import_csv(con, path, sy), os.path.basename(path))
            except Exception as exc:  # noqa: BLE001
                _log(f"{os.path.basename(path)}: помилка ({exc}) ❌")
        for idx in ("CREATE INDEX IF NOT EXISTS ix_plate ON vehicle_ops(plate)",
                    "CREATE INDEX IF NOT EXISTS ix_vin ON vehicle_ops(vin)",
                    "CREATE INDEX IF NOT EXISTS ix_digits ON vehicle_ops(digits)",
                    "CREATE INDEX IF NOT EXISTS ix_le ON vehicle_ops(letters_end)",
                    "CREATE INDEX IF NOT EXISTS ix_hash ON vehicle_ops(row_hash)"):
            con.execute(idx)
        con.commit()
        con.close()
        with _lock:
            STATE["db_rows"] = _db_count()
        cols_msg = f" · нових колонок: {len(set(allcols))} ({', '.join(sorted(set(allcols)))})" if allcols else ""
        _log(f"{source} завершено: ➕ додано {agg['added']} · 🔄 доповнено {agg['enriched']} · "
             f"⏭ дублікатів {agg['dup']}{cols_msg}. Усього в базі: {STATE['db_rows']}.")
    except Exception as exc:  # noqa: BLE001
        _log(f"Помилка імпорту: {exc}")
    finally:
        with _lock:
            STATE["loading"] = False


def _import_folder() -> None:
    """Залити всі CSV/ZIP з налаштованої папки додаткових дампів у наявну базу."""
    folder = IMPORT_DIR
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    files = ([os.path.join(folder, f) for f in sorted(os.listdir(folder))
              if f.lower().endswith((".csv", ".zip"))] if os.path.isdir(folder) else [])
    if not files:
        _log(f"У папці додаткових дампів файлів нема: {folder}")
        return
    _import_paths(files, source="Імпорт з папки")


def _import_uploaded(filename, data) -> None:
    """Залити ОДИН завантажений через панель файл (CSV/ZIP) у базу (дедуп+доповнення)."""
    import shutil
    safe = os.path.basename(filename or "upload.dat") or "upload.dat"
    if not safe.lower().endswith((".csv", ".zip")):
        _log(f"Пропущено {safe}: підтримуються лише CSV/ZIP.")
        return
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, safe)
    try:
        with open(path, "wb") as fh:
            fh.write(data or b"")
        _import_paths([path], source=f"Заливка «{safe}»")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


_KNOWN_COLS = {"vin", "plate", "brand", "model", "make_year", "color", "kind", "body", "fuel",
               "capacity", "purpose", "own_weight", "total_weight", "d_reg", "oper_code", "oper_name",
               "dep_code", "dep", "reg_addr_koatuu", "person", "src_year", "letters_start", "digits",
               "letters_end", "row_hash"}


def _lookup(plate=None, vin=None) -> dict:
    # Сортуємо за роком дампу (надійний тег), потім за датою; рядки без дати (нові
    # переоформлення часто без D_REG) — В КІНЦІ року, бо вони найсвіжіші. Так останній
    # рядок = ПОТОЧНЕ авто (номер міг переходити з машини на машину).
    order = "ORDER BY src_year, (d_reg IS NULL), d_reg"
    con = _connect()
    try:
        if plate:
            rows = con.execute("SELECT * FROM vehicle_ops WHERE plate=? " + order,
                               (_norm_plate(plate),)).fetchall()
        elif vin:
            rows = con.execute("SELECT * FROM vehicle_ops WHERE vin=? " + order,
                               ((vin or "").strip().upper(),)).fetchall()
        else:
            return {"found": False}
    finally:
        con.close()
    if not rows:
        return {"found": False}
    last = rows[-1]  # найсвіжіша операція = поточне авто на цьому номері
    veh = {k: last[k] for k in ("vin", "plate", "brand", "model", "make_year", "color",
                                "kind", "body", "fuel", "capacity")}
    dates = [r["d_reg"] for r in rows if r["d_reg"]]
    first_reg = min(dates) if dates else None
    # Історія — найновіше зверху; з маркою/VIN кожного рядка (видно, як номер міняв авто).
    history = [{"d_reg": r["d_reg"], "oper_name": r["oper_name"], "dep": r["dep"],
                "plate": r["plate"], "vin": r["vin"], "brand": r["brand"],
                "model": r["model"], "make_year": r["make_year"]} for r in reversed(rows)]
    # Будь-які ДОДАТКОВІ поля (юр.особа, відмітки, власники…), залиті універсальним імпортом.
    extra = {k: last[k] for k in last.keys()
             if k not in _KNOWN_COLS and last[k] not in (None, "")}
    return {"found": True, "vehicle": veh, "first_reg": first_reg, "history": history, "extra": extra}


def _lookup_digits(digits, series=None, regions=None, limit=400) -> list:
    """Зайняті номери із заданою комбінацією цифр (+ опц. серія/регіон). Кожен → поточне авто."""
    digits = (digits or "").strip()
    if not digits:
        return []
    q = ("SELECT plate, vin, brand, model, make_year, letters_start, letters_end, src_year, d_reg "
         "FROM vehicle_ops WHERE digits=?")
    params = [digits]
    if series:
        q += " AND letters_end IN (%s)" % ",".join("?" * len(series))
        params += list(series)
    if regions:
        q += " AND letters_start IN (%s)" % ",".join("?" * len(regions))
        params += list(regions)
    con = _connect()
    try:
        rows = con.execute(q, params).fetchall()
    finally:
        con.close()
    best = {}  # plate → (sort_key, row) — найсвіжіша операція = поточне авто
    for r in rows:
        p = r["plate"]
        if not p:
            continue
        key = (r["src_year"] or 0, r["d_reg"] or "")
        if p not in best or key > best[p][0]:
            best[p] = (key, r)
    out = [{"plate": p, "vin": r["vin"], "brand": r["brand"], "model": r["model"],
            "make_year": r["make_year"]} for p, (k, r) in best.items()]
    out.sort(key=lambda x: x["plate"])
    return out[:limit]


def _poll_once() -> bool:
    """Один цикл: спитати сервер про запит у черзі, відповісти на нього. True — якщо обробив запит."""
    body = json.dumps({"secret": SECRET}).encode("utf-8")
    req = urllib.request.Request(SERVER_URL.rstrip("/") + "/autocheck/poll", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    rq = data.get("req")
    if not rq:
        return False
    try:
        if rq.get("digits"):  # запит «зайняті за комбінацією»
            occ = _lookup_digits(rq.get("digits"), rq.get("series"), rq.get("regions"))
            result = {"found": bool(occ), "occupied": occ}
        else:
            result = _lookup(plate=rq.get("plate") or None, vin=rq.get("vin") or None)
    except Exception as exc:  # noqa: BLE001
        result = {"found": False, "error": str(exc)}
    out = json.dumps({"secret": SECRET, "id": rq.get("id"), "result": result}).encode("utf-8")
    rr = urllib.request.Request(SERVER_URL.rstrip("/") + "/autocheck/result", data=out,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rr, timeout=30):
        pass
    return True


def _poll_loop() -> None:
    """Постійно опитувати сервер на запити перевірки (лише ВИХІДНІ запити — без тунеля/портів)."""
    with _lock:
        if STATE["polling"]:
            return
        STATE["polling"] = True
    _log("Підключено до бота (режим опитування). Чекаю запити…")
    fails = 0
    while True:
        try:
            handled = _poll_once()
            with _lock:
                STATE["connected"] = True
            fails = 0
            if handled:
                with _lock:
                    STATE["answered"] += 1
                    n = STATE["answered"]
                _log(f"Відповів на запит перевірки ✅ (усього {n})")
        except Exception as exc:  # noqa: BLE001
            fails += 1
            with _lock:
                STATE["connected"] = False
            _log(f"Звʼязок із сервером перервано ({exc}); повтор за 5с…")
            time.sleep(min(5 + fails, 30))


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
<button onclick="j('/api/build','POST')">🛠 Побудувати базу з осн. дампів</button>
<button onclick="j('/api/import','POST')">📥 Імпортувати додаткові дампи</button>
<button onclick="j('/api/load','POST')">⬇️ Завантажити з мережі</button>
<button class=g onclick="j('/api/connect','POST')">🔗 Підключити до бота</button>
<div id=prog></div>
<div id=stat class=muted style="margin-top:8px"></div>
</div>
<div class=bar>
<b>⚙️ Шляхи</b> <span class=muted>(де база й де брати дампи)</span><br>
<div style="margin-top:6px">📁 Папка бази: <input id=p_db style="width:360px"></div>
<div style="margin-top:6px">📁 Основні дампи (reestr МВС): <input id=p_dumps style="width:360px"></div>
<div style="margin-top:6px">📁 Додаткові дампи (для імпорту): <input id=p_import style="width:360px"></div>
<button class=g style="margin-top:8px" onclick="savePaths()">💾 Зберегти шляхи</button>
</div>
<div class=bar>
<b>📎 Доповнити базу файлом</b> <span class=muted>(вибери CSV/ZIP, можна кілька)</span><br>
<input type=file id=upl multiple accept=".csv,.zip" style="width:auto;padding:4px;margin-top:6px">
<button class=g onclick="uploadFiles()">➕ Доповнити базу вибраним</button>
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
async function savePaths(){
 const b={db_dir:document.getElementById('p_db').value.trim(),
          dumps_dir:document.getElementById('p_dumps').value.trim(),
          import_dir:document.getElementById('p_import').value.trim()};
 await fetch('/api/paths',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 tick();}
async function uploadFiles(){
 const inp=document.getElementById('upl'); if(!inp.files.length){alert('Оберіть файл(и) CSV/ZIP');return;}
 for(const f of inp.files){
  document.getElementById('prog').textContent='Заливаю '+f.name+'… (не закривай вікно)';
  await fetch('/api/upload',{method:'POST',headers:{'X-Filename':encodeURIComponent(f.name)},body:f});
 }
 inp.value=''; tick();}
async function tick(){const s=await j('/api/state');
 document.getElementById('prog').textContent=s.progress||'';
 if(s.paths){for(const p of [['db_dir','p_db'],['dumps_dir','p_dumps'],['import_dir','p_import']]){
  const el=document.getElementById(p[1]); if(el&&document.activeElement!==el) el.value=s.paths[p[0]]||'';}}
 document.getElementById('stat').innerHTML='База: <b>'+(s.db_rows||0)+'</b> рядків. '+
  (s.connected?('✅ Підключено до бота · відповів на запитів: '+(s.answered||0)):
   (s.polling?'Підключаюсь…':'Не підключено.'));
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
                            "polling": STATE["polling"], "connected": STATE["connected"],
                            "answered": STATE["answered"], "paths": _load_paths()})
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
        elif self.path == "/api/import":
            threading.Thread(target=_import_folder, daemon=True).start()
            self._send({"started": True})
        elif self.path == "/api/upload":
            from urllib.parse import unquote
            fn = unquote(self.headers.get("X-Filename", "upload.dat"))
            ln = int(self.headers.get("Content-Length", 0) or 0)
            data = self.rfile.read(ln) if ln else b""
            _import_uploaded(fn, data)  # синхронно — фронт чекає завершення заливки
            self._send({"ok": True, "name": fn})
        elif self.path == "/api/paths":
            ln = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
            except Exception:  # noqa: BLE001
                body = {}
            applied = _save_paths(body)
            with _lock:
                STATE["db_rows"] = _db_count()
            _log("Шляхи збережено. База: " + applied["db_dir"])
            self._send({"ok": True, "paths": applied})
        elif self.path == "/api/connect":
            threading.Thread(target=_poll_loop, daemon=True).start()
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
        _poll_loop()
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
