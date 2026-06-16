"""Plate normalisation/parsing for the local MVP (Python 3.9 compatible, no deps)."""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

_PLATE_RE = re.compile(r"^([A-ZА-ЯІЇЄҐ]*)(\d+)([A-ZА-ЯІЇЄҐ]*)$")

# Portal renders Latin glyphs; canonicalise to Cyrillic so Cyrillic hunts match.
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "I": "І",
        "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х",
    }
)


def normalize_plate(raw: str) -> str:
    """Uppercase, strip separators, transliterate Latin→Cyrillic."""
    cleaned = re.sub(r"[\s\-]", "", raw or "").strip().upper()
    return cleaned.translate(_LATIN_TO_CYRILLIC)


def parse_plate(raw: str) -> Dict[str, Optional[object]]:
    """Split a plate into letters_start/digits/letters_end + digits_int."""
    number = normalize_plate(raw)
    m = _PLATE_RE.match(number)
    if not m:
        return {
            "plate_number": number, "letters_start": None, "letters_end": None,
            "digits": None, "digits_int": None,
        }
    start, digits, end = m.groups()
    digits_int: Optional[int] = None
    if digits:
        try:
            digits_int = int(digits)
        except ValueError:
            digits_int = None
    return {
        "plate_number": number,
        "letters_start": start or None,
        "letters_end": end or None,
        "digits": digits or None,
        "digits_int": digits_int,
    }


def to_search_like(query: str) -> Tuple[str, str]:
    """Convert a user query into a SQL LIKE target, supporting digit-position wildcards.

    ``-`` and ``*`` mean "any single digit". Rules:
      * Digit mask (only digits and wildcards) → match the 4-digit ``digits`` column.
        Shorter masks are right-padded with wildcards, so "12" → "12__", "1--4" → "1__4",
        "**34" → "__34". This lets the user pin the 1st/2nd/3rd/4th digit in any combination.
      * Anything containing letters → substring match on ``plate_number`` (wildcards → '_').

    Returns:
        (mode, pattern) where mode is "digits" or "plate".
    """
    q = re.sub(r"\s", "", query or "").upper().translate(_LATIN_TO_CYRILLIC)
    q = q.replace("*", "_").replace("-", "_")
    core = q.replace("_", "")
    if q and (core.isdigit() or core == ""):
        mask = q[:4]
        if len(mask) < 4:
            mask = mask + "_" * (4 - len(mask))
        return "digits", mask
    return "plate", "%" + q + "%"


def pattern_to_match(pattern_raw: str) -> Tuple[str, Dict[str, Optional[str]]]:
    """Infer (match_type, fields) from a user pattern.

    Supports: digit masks (0*00, 1--4 → digits_mask), pure digits (1234 → digits),
    letter wildcards (АА****, ****ВВ, АА****ВВ), exact plates (АА1234ВВ).
    Both ``*`` and ``-`` are single-character wildcards.
    """
    p = re.sub(r"\s", "", pattern_raw or "").strip().upper().translate(_LATIN_TO_CYRILLIC)
    p = p.replace("-", "*")  # unify wildcards
    has_star = "*" in p
    core = p.replace("*", "")
    # Digit mask: only digits + wildcards (e.g. 0*00) → match the digits column.
    if core.isdigit() and core != "" and has_star:
        return "digits_mask", {"digits_mask": p.replace("*", "_"), "pattern": p}
    if core.isdigit() and not has_star:
        return "digits", {"digits": core, "pattern": p}
    if not has_star:
        return "exact", {"pattern": p}
    lead = re.match(r"^([А-ЯІЇЄҐ\d]*)\*+", p)
    trail = re.search(r"\*+([А-ЯІЇЄҐ\d]*)$", p)
    start = lead.group(1) if lead else ""
    end = trail.group(1) if trail else ""
    if start and end:
        return "combined", {"letters_start": start, "letters_end": end, "pattern": p}
    if start:
        return "starts", {"letters_start": start, "pattern": p}
    if end:
        return "ends", {"letters_end": end, "pattern": p}
    return "contains", {"pattern": p}
