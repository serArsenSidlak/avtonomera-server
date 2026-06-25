"""Автономера — конвертер дампів МВС у локальну базу (Windows .exe).

Поклади цей файл (BuildAutoCheckDB.exe) у папку із завантаженими дампами МВС
(reestrTZ*.zip / tz_opendata*.zip / *.csv) і запусти. Він:
  • перебирає ВСІ знайдені дампи (zip і csv), включно зі старим форматом 2013–2025
    (з номером N_REG_NEW) і новим 2026 (без номера, об'єднана операція, 2-значний рік);
  • будує локальну SQLite-базу `autocheck.db` у %LOCALAPPDATA%\\AvtonomeraAutoCheck\\
    (звідти її автоматично віддає AutoCheck-агент у бот);
  • створює індекси по номеру і VIN для миттєвого пошуку.

Можна також перетягнути файли/папки дампів прямо на .exe.
"""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import sys
import time
import zipfile

_LAT2CYR = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
                          "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х"})
COLUMNS = ["vin", "plate", "brand", "model", "make_year", "color", "kind", "body", "purpose",
           "fuel", "capacity", "own_weight", "total_weight", "d_reg", "oper_code", "oper_name",
           "dep_code", "dep", "reg_addr_koatuu", "person", "src_year"]
_OPER_COMBINED = "CD.OPER_CODE||'-'||CD.OPERAS"  # нова (2026) об'єднана колонка операції


def _appdir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "AvtonomeraAutoCheck")
    os.makedirs(d, exist_ok=True)
    return d


DB_PATH = os.path.join(_appdir(), "autocheck.db")


def _norm_plate(raw):
    p = re.sub(r"[\s\-]", "", (raw or "")).strip().upper().translate(_LAT2CYR)
    return p or None


def _int(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        return None


def _iso(v):
    """dd.mm.yyyy або dd.mm.yy → ISO YYYY-MM-DD."""
    v = (v or "").strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2,4})", v)
    if m:
        y = m.group(3)
        y = "20" + y if len(y) == 2 else y
        return f"{y}-{m.group(2)}-{m.group(1)}"
    if re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return None


def _year_from_name(name):
    m = re.search(r"(20\d{2})", os.path.basename(name))
    return int(m.group(1)) if m else None


def _record(row, src_year):
    """Один рядок дампу → кортеж у порядку COLUMNS. Підтримує старий і новий (2026) формати."""
    opn = row.get("OPER_NAME")
    opc = _int(row.get("OPER_CODE"))
    comb = row.get(_OPER_COMBINED) or ""
    if not opn and comb:  # новий формат: "50 - ПЕРЕРЕЄСТРАЦІЯ…"
        parts = comb.split(" - ", 1)
        opc = _int(parts[0]) if opc is None else opc
        opn = parts[1] if len(parts) > 1 else comb
    plate = _norm_plate(row.get("N_REG_NEW")) if row.get("N_REG_NEW") else None
    return (
        (row.get("VIN") or "").strip() or None, plate,
        (row.get("BRAND") or "").strip() or None, (row.get("MODEL") or "").strip() or None,
        _int(row.get("MAKE_YEAR")), (row.get("COLOR") or "").strip() or None,
        (row.get("KIND") or "").strip() or None, (row.get("BODY") or "").strip() or None,
        (row.get("PURPOSE") or "").strip() or None, (row.get("FUEL") or "").strip() or None,
        _int(row.get("CAPACITY")), _int(row.get("OWN_WEIGHT")), _int(row.get("TOTAL_WEIGHT")),
        _iso(row.get("D_REG")), opc, opn, _int(row.get("DEP_CODE")),
        (row.get("DEP") or "").strip() or None, (row.get("REG_ADDR_KOATUU") or "").strip() or None,
        (row.get("PERSON") or "").strip() or None, src_year,
    )


def _iter_csv_streams(path):
    """Yield (name, text_stream) для кожного CSV — із zip (без розпакування) або прямого .csv."""
    low = path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                if n.lower().endswith(".csv"):
                    with zf.open(n) as raw:
                        yield n, io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    elif low.endswith(".csv"):
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            yield os.path.basename(path), fh


def _find_dumps(args):
    """Зібрати список файлів дампів: з аргументів (drag-drop) + папка біля exe + ./dumps."""
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    roots, files = [], []
    for a in args:
        if os.path.isdir(a):
            roots.append(a)
        elif os.path.isfile(a):
            files.append(a)
    roots += [here, os.path.join(here, "dumps")]
    for r in roots:
        if os.path.isdir(r):
            for f in os.listdir(r):
                p = os.path.join(r, f)
                if os.path.isfile(p) and (f.lower().endswith(".zip") or f.lower().endswith(".csv")):
                    files.append(p)
    # унікальні, без самого exe
    seen, out = set(), []
    for f in files:
        rp = os.path.realpath(f)
        if rp not in seen and not rp.lower().endswith(".exe"):
            seen.add(rp)
            out.append(f)
    return sorted(out)


def main():
    print("=" * 56)
    print("  Автономера — конвертер дампів МВС → autocheck.db")
    print("=" * 56)
    dumps = _find_dumps(sys.argv[1:])
    if not dumps:
        print("\n❌ Дампів не знайдено.")
        print("Поклади файли reestrTZ*.zip / *.csv поряд із цим .exe (або в підпапку 'dumps'),")
        print("або перетягни їх прямо на .exe, і запусти ще раз.")
        input("\nEnter — вийти…")
        return
    print(f"\nЗнайдено дампів: {len(dumps)}")
    for d in dumps:
        print("  •", os.path.basename(d))
    print(f"\nБаза: {DB_PATH}")

    t0 = time.time()
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE vehicle_ops (" + ", ".join(
            c + (" INTEGER" if c in ("make_year", "capacity", "own_weight", "total_weight",
                                     "oper_code", "dep_code", "src_year") else " TEXT")
            for c in COLUMNS) + ")")
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    ins = f"INSERT INTO vehicle_ops ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"
    total = 0
    for path in dumps:
        sy = _year_from_name(path)
        cnt = 0
        try:
            for name, stream in _iter_csv_streams(path):
                batch = []
                for row in csv.DictReader(stream, delimiter=";"):
                    batch.append(_record(row, sy))
                    if len(batch) >= 20000:
                        con.executemany(ins, batch)
                        cnt += len(batch)
                        batch = []
                        print(f"\r  {os.path.basename(path)}: {cnt:,} рядків…", end="", flush=True)
                if batch:
                    con.executemany(ins, batch)
                    cnt += len(batch)
            con.commit()
            total += cnt
            print(f"\r  ✅ {os.path.basename(path)}: {cnt:,} рядків" + " " * 10)
        except Exception as exc:  # noqa: BLE001
            print(f"\r  ❌ {os.path.basename(path)}: помилка ({exc})")
    print("\nБудую індекси (номер, VIN)…")
    con.execute("CREATE INDEX IF NOT EXISTS ix_plate ON vehicle_ops(plate)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_vin ON vehicle_ops(vin)")
    con.commit()
    con.close()
    size_mb = os.path.getsize(DB_PATH) / 1048576
    print(f"\n✅ Готово! Усього {total:,} рядків за {time.time() - t0:.0f} c. Розмір бази: {size_mb:.0f} МБ.")
    print(f"База: {DB_PATH}")
    print("\nТепер запусти AutoCheckAgent.exe → «🔗 Підключити до бота» — і перевірка авто запрацює.")
    input("\nEnter — вийти…")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print("Помилка:", exc)
        input("Enter — вийти…")
