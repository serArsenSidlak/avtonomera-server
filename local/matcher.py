"""Pure hunt-matching for the local MVP (Python 3.9, no deps)."""
from __future__ import annotations

import re
from typing import Any, Dict


def _pattern_ok(plate: Dict[str, Any], hunt: Dict[str, Any]) -> bool:
    """Whether the plate string satisfies the hunt's pattern by match_type."""
    number = (plate.get("plate_number") or "").upper()
    mt = hunt.get("match_type")
    digits = plate.get("digits")

    if mt == "filters":
        if hunt.get("letters_start") and (plate.get("letters_start") or "").upper() != hunt["letters_start"].upper():
            return False
        if hunt.get("digits_exact"):
            return digits == hunt["digits_exact"]
        if hunt.get("digits_mask"):
            return bool(digits) and re.fullmatch(hunt["digits_mask"].replace("_", "."), digits) is not None
        return True  # series/region/type/price-only hunt (filters checked separately)
    if mt == "digits_mask":
        mask = hunt.get("digits_mask")
        if not mask or not digits:
            return False
        # '_' (SQL single-char wildcard) → regex '.'
        return re.fullmatch(mask.replace("_", "."), digits) is not None
    if mt == "exact":
        pat = (hunt.get("pattern") or "").replace("*", "").upper()
        return bool(pat) and number == pat
    if mt == "starts":
        ls = hunt.get("letters_start")
        return bool(ls) and number.startswith(ls.upper())
    if mt == "ends":
        le = hunt.get("letters_end")
        return bool(le) and number.endswith(le.upper())
    if mt == "contains":
        pat = (hunt.get("pattern") or "").replace("*", "").upper()
        return bool(pat) and pat in number
    if mt == "digits":
        if hunt.get("digits_exact"):
            return digits == hunt["digits_exact"]
        if hunt.get("digits_contains"):
            return bool(digits) and hunt["digits_contains"] in digits
        return False
    if mt == "combined":
        ok = True
        if hunt.get("letters_start"):
            ok = ok and number.startswith(hunt["letters_start"].upper())
        if hunt.get("letters_end"):
            ok = ok and number.endswith(hunt["letters_end"].upper())
        if hunt.get("digits_exact"):
            ok = ok and digits == hunt["digits_exact"]
        return ok and any(
            hunt.get(k) for k in ("letters_start", "letters_end", "digits_exact")
        )
    return False


def _filters_ok(plate: Dict[str, Any], hunt: Dict[str, Any]) -> bool:
    """Whether the plate passes the hunt's secondary filters."""
    if hunt.get("region") and plate.get("region") != hunt["region"]:
        return False
    if hunt.get("vehicle_type") and plate.get("vehicle_type") != hunt["vehicle_type"]:
        return False
    price = plate.get("price")
    if hunt.get("price_min") is not None and (price is None or price < hunt["price_min"]):
        return False
    if hunt.get("price_max") is not None and (price is None or price > hunt["price_max"]):
        return False
    return True


def matches(plate: Dict[str, Any], hunt: Dict[str, Any]) -> bool:
    """Return True if the plate matches both the hunt pattern and its filters."""
    return _pattern_ok(plate, hunt) and _filters_ok(plate, hunt)
