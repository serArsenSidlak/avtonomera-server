"""Автономера — однокнопковий парсер ОДНІЄЇ області (opendata.hsc.gov.ua).

Який регіон сканувати визначається з ІМЕНІ ФАЙЛУ (напр. «Львівська.exe» → Львівська) або з
першого аргументу командного рядка. Подвійний клік → відкривається браузер, парситься своя
область, дані відправляються на сервер (/ingest), база оновлюється. Без панелі, без вибору —
просто запусти потрібний файл. Можна тримати різні файли на різних ПК.

⚠️ Працює лише з українського IP (Akamai пускає тільки UA) і коли портал opendata живий.

Збірка: один .exe, CI копіює його під кожну область. Регіон береться з назви копії.
"""
from __future__ import annotations

import os
import sys
import time

# Reuse the proven scraper/uploader from the panel agent.
import scrape_agent as A

# Canonical region names (must match the opendata #region dropdown; fuzzy-matched if a suffix
# like "область" differs). These are the regions we ship per-region executables for.
REGIONS = [
    "Вінницька", "Волинська", "Дніпропетровська", "Донецька", "Житомирська",
    "Закарпатська", "Запорізька", "Івано-Франківська", "Київська", "Кіровоградська",
    "Львівська", "Миколаївська", "Одеська", "Полтавська", "Рівненська", "Сумська",
    "Тернопільська", "Харківська", "Херсонська", "Хмельницька", "Черкаська",
    "Чернівецька", "Чернігівська", "м. Київ",
]


# Latin aliases so the exe can also be renamed without Cyrillic (e.g. "Lvivska.exe").
ALIASES = {
    "vinnytska": "Вінницька", "volynska": "Волинська", "dnipropetrovska": "Дніпропетровська",
    "dnipro": "Дніпропетровська", "donetska": "Донецька", "zhytomyrska": "Житомирська",
    "zakarpatska": "Закарпатська", "zaporizka": "Запорізька", "ivanofrankivska": "Івано-Франківська",
    "kyivska": "Київська", "kirovohradska": "Кіровоградська", "lvivska": "Львівська", "lviv": "Львівська",
    "mykolaivska": "Миколаївська", "odeska": "Одеська", "odesa": "Одеська", "poltavska": "Полтавська",
    "rivnenska": "Рівненська", "sumska": "Сумська", "ternopilska": "Тернопільська",
    "kharkivska": "Харківська", "kharkiv": "Харківська", "khersonska": "Херсонська",
    "khmelnytska": "Хмельницька", "cherkaska": "Черкаська", "chernivetska": "Чернівецька",
    "chernihivska": "Чернігівська", "mkyiv": "м. Київ", "kyiv": "м. Київ", "kyivcity": "м. Київ",
}


def _resolve_region() -> str | None:
    """Pick the target region from argv[1] or the executable's own file name."""
    query = sys.argv[1] if len(sys.argv) > 1 else os.path.splitext(os.path.basename(sys.argv[0]))[0]
    qn = A._norm_region(query)
    if not qn:
        return None
    for r in REGIONS:  # exact (normalised) first
        if A._norm_region(r) == qn:
            return r
    if qn in ALIASES:  # Latin alias (Lvivska, Odesa, Kyiv…)
        return ALIASES[qn]
    for r in REGIONS:  # then partial Cyrillic
        rn = A._norm_region(r)
        if qn in rn or rn in qn:
            return r
    return None


def main() -> None:
    print("=" * 48, flush=True)
    print("  Автономера — оновлення бази (одна область)", flush=True)
    print("=" * 48, flush=True)
    region = _resolve_region()
    if not region:
        print(f"\n❌ Не вдалося визначити область з імені файлу: {os.path.basename(sys.argv[0])}", flush=True)
        print("   Перейменуй файл на назву області (напр. «Львівська.exe»)", flush=True)
        print(f"   Доступні: {', '.join(REGIONS)}", flush=True)
        input("\nНатисни Enter, щоб закрити…")
        return
    if not A.INGEST_SECRET:
        print("\n⚠️ Немає INGEST_SECRET — відправка на сервер не спрацює (це збірка без секрету).", flush=True)

    A._ensure_chromium()
    print(f"\n▶️ Сканую область «{region}» з opendata.hsc.gov.ua …", flush=True)
    print("   (відкриється вікно браузера — не закривай його, працює саме)\n", flush=True)
    t0 = time.time()
    try:
        A._run_scan([region])  # headed Playwright; fills STATE["cache"][region]
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Помилка скану: {exc}", flush=True)
        input("\nНатисни Enter, щоб закрити…")
        return

    st = A.STATE["regions"].get(region, {})
    if st.get("status") == "ok" and st.get("count"):
        sent = A._send([region])
        print(f"\n✅ Готово за {int(time.time() - t0)} c: зібрано {st['count']} номерів, "
              f"надіслано на сервер ~{sent}. Базу оновлено.", flush=True)
    else:
        print("\n❌ Не вдалося зібрати дані. Можливі причини:", flush=True)
        print("   • IP не український (Akamai блокує) — увімкни UA-інтернет/VPN", flush=True)
        print("   • портал opendata зараз недоступний — спробуй пізніше", flush=True)
    input("\nНатисни Enter, щоб закрити…")


if __name__ == "__main__":
    main()
