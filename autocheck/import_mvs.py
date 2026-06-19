"""Завантажувач реєстру МВС у БД AutoCheck.

Качає річні ZIP-архіви набору data.gov.ua «Відомості про транспортні засоби та їх власників»,
розпаковує CSV (роздільник ';', UTF-8), нормалізує і масово заливає в таблицю `vehicle_ops`
через asyncpg COPY. Працює з ОКРЕМОЮ базою (не Supabase) — DSN із env `AUTOCHECK_DSN`.

Запуск:
    AUTOCHECK_DSN=postgresql://user:pass@host:5432/autocheck python -m autocheck.import_mvs 2023 2024 2025
    AUTOCHECK_DSN=...                                       python -m autocheck.import_mvs all

Перед першим запуском застосуй схему:  psql "$AUTOCHECK_DSN" -f autocheck/schema.sql
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from typing import Iterator, Optional

import asyncpg

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


def _norm_plate(raw: str) -> Optional[str]:
    p = re.sub(r"[\s\-]", "", (raw or "")).strip().upper().translate(_LAT2CYR)
    return p or None


def _int(v: str) -> Optional[int]:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v.replace(",", ".")))
    except ValueError:
        return None


def _date(v: str):
    import datetime as dt

    v = (v or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "avtonomera-autocheck/1.0"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)


def _rows(csv_path: str, year: int) -> Iterator[tuple]:
    """Yield normalized records (tuples in COLUMNS order) from one MVS CSV file."""
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
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
                _date(row.get("D_REG")),
                _int(row.get("OPER_CODE")),
                (row.get("OPER_NAME") or "").strip() or None,
                _int(row.get("DEP_CODE")),
                (row.get("DEP") or "").strip() or None,
                (row.get("REG_ADDR_KOATUU") or "").strip() or None,
                (row.get("PERSON") or "").strip() or None,
                year,
            )


async def import_year(pool: asyncpg.Pool, year: int, url: str) -> int:
    """Download a year archive, parse its CSV and COPY all rows into vehicle_ops."""
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, f"{year}.zip")
        print(f"[{year}] завантажую {url} …", flush=True)
        _download(url, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                print(f"[{year}] немає CSV у архіві — пропускаю", flush=True)
                return 0
            zf.extractall(tmp)
        total = 0
        for name in names:
            csv_path = os.path.join(tmp, name)
            records = list(_rows(csv_path, year))
            async with pool.acquire() as con:
                await con.copy_records_to_table("vehicle_ops", records=records, columns=COLUMNS)
                await con.execute(
                    "INSERT INTO import_log(src_year, resource_id, file_name, rows) "
                    "VALUES($1,$2,$3,$4)", year, url.split("/resource/")[-1].split("/")[0],
                    os.path.basename(name), len(records))
            total += len(records)
            print(f"[{year}] {os.path.basename(name)}: залито {len(records)} рядків", flush=True)
    return total


async def main(years: list) -> None:
    dsn = os.environ.get("AUTOCHECK_DSN")
    if not dsn:
        print("ERROR: set AUTOCHECK_DSN", file=sys.stderr)
        sys.exit(1)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        grand = 0
        for y in years:
            url = RESOURCES.get(y)
            if not url:
                print(f"[{y}] немає ресурсу — пропускаю", flush=True)
                continue
            grand += await import_year(pool, y, url)
        print(f"Готово. Усього залито {grand} рядків.", flush=True)
    finally:
        await pool.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("usage: python -m autocheck.import_mvs <year ...|all>", file=sys.stderr)
        sys.exit(1)
    sel = sorted(RESOURCES) if args == ["all"] else [int(a) for a in args]
    asyncio.run(main(sel))
