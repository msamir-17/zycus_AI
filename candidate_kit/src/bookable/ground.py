"""
src/bookable/ground.py

Responsibility: Deterministic validation and numeric normalisation of financial amounts.
Uses decimal.Decimal for all money calculations.
Strictly distinguishes financial amounts from dates, PO numbers, and codes.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

_CURRENCY_SYMBOLS = set("€$£¥₹ \t\xa0")
_CURRENCY_WORDS = {
    "EUR", "USD", "GBP", "THB", "SGD", "ZAR", "MYR", "CHF", "AUD", "CAD",
    "KES", "GHC", "GHS", "DKK", "NOK", "SEK", "PLN"
}


def strip_financial_decorations(raw: str) -> str:
    """Strip currency symbols, words, % and whitespace from a candidate financial amount."""
    s = str(raw).strip()
    # Remove leading/trailing currency words
    for cw in _CURRENCY_WORDS:
        if s.upper().startswith(cw):
            s = s[len(cw):].strip()
        if s.upper().endswith(cw):
            s = s[:-len(cw)].strip()

    # South African Rand prefix: e.g. "R148,941.47" or "R 129,514.32"
    if re.match(r"^R\s*\d", s, re.IGNORECASE):
        s = re.sub(r"^R\s*", "", s, count=1, flags=re.IGNORECASE).strip()
            
    # Remove leading symbols
    i = 0
    while i < len(s) and s[i] in _CURRENCY_SYMBOLS:
        i += 1
    s = s[i:].strip()
    
    # Remove trailing symbols and %
    j = len(s)
    while j > 0 and (s[j - 1] in _CURRENCY_SYMBOLS or s[j - 1] == "%"):
        j -= 1
    return s[:j].strip()


def normalise_financial_amount(raw: Any) -> Tuple[Optional[Decimal], str, bool]:
    """
    Deterministically normalise a financial amount string to Decimal and dot-decimal string.
    
    Returns:
        (decimal_value, normalised_dot_decimal_string, is_ambiguous)
        
    Rules:
    - Dot-thousands, comma-decimal (e.g. "1.234,56", "327,87", "771,66") -> converted to dot-decimal.
    - Comma-thousands, dot-decimal (e.g. "1,234.56", "17,657.53") -> converted to dot-decimal.
    - Space-thousands (e.g. "152 587.46", "152 587,46") -> converted to dot-decimal.
    - Plain integer or dot-decimal (e.g. "77166", "12.50") -> parsed as-is.
    - Ambiguous (e.g. "123.456" or "123,456" with exactly 3 digits after single separator) ->
      flagged as ambiguous, does not guess, returns None decimal and preserves raw.
    """
    if raw is None or raw == "":
        return None, "", False

    if isinstance(raw, (int, float, Decimal)):
        try:
            d = Decimal(str(raw))
            return d, str(d), False
        except InvalidOperation:
            return None, str(raw), False

    cleaned = strip_financial_decorations(str(raw))
    if not cleaned:
        return None, "", False

    # Check for negative sign
    is_negative = False
    if cleaned.startswith("-") or cleaned.startswith("("):
        is_negative = True
        cleaned = cleaned.lstrip("-(").rstrip(")")

    # 1. European format with period thousands and comma decimal: e.g. "1.234,56" or "12.345.678,90"
    if re.match(r"^\d{1,3}(?:\.\d{3})+,\d{1,4}$", cleaned):
        num_str = cleaned.replace(".", "").replace(",", ".")
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 2. Standard format with comma thousands and period decimal: e.g. "1,234.56" or "12,345,678.90"
    if re.match(r"^\d{1,3}(?:,\d{3})+\.\d{1,4}$", cleaned):
        num_str = cleaned.replace(",", "")
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 3. Space-separated thousands with period decimal: e.g. "152 587.46"
    if re.match(r"^\d{1,3}(?:\s\d{3})+\.\d{1,4}$", cleaned):
        num_str = re.sub(r"\s+", "", cleaned)
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 4. Space-separated thousands with comma decimal: e.g. "152 587,46"
    if re.match(r"^\d{1,3}(?:\s\d{3})+,\d{1,4}$", cleaned):
        num_str = re.sub(r"\s+", "", cleaned).replace(",", ".")
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 3. Comma-decimal without thousands separators: e.g. "771,66", "327,87", "0,05"
    # Note: 1, 2, or 4 decimal places are unambiguous decimal commas (thousands groups are always 3 digits)
    if re.match(r"^\d+,\d{1,2}$", cleaned) or re.match(r"^\d+,\d{4}$", cleaned):
        num_str = cleaned.replace(",", ".")
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 4. Period-decimal without thousands separators: e.g. "771.66", "327.87", "0.05"
    if re.match(r"^\d+\.\d{1,2}$", cleaned) or re.match(r"^\d+\.\d{4,}$", cleaned):
        num_str = cleaned
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 5. Plain integer: e.g. "77166", "100"
    if re.match(r"^\d+$", cleaned):
        num_str = cleaned
        try:
            val = Decimal(f"-{num_str}" if is_negative else num_str)
            return val, f"-{num_str}" if is_negative else num_str, False
        except InvalidOperation:
            return None, str(raw), True

    # 6. Ambiguous cases: single separator followed by exactly 3 digits (e.g. "123,456" or "123.456")
    # Could be 123 thousand 456, or 123 point 456. Do NOT guess!
    if re.match(r"^\d{1,3}[,\.]\d{3}$", cleaned):
        return None, str(raw), True

    # Fallback / unparseable
    return None, str(raw), False


def derive_unit_price(
    quantity_raw: Any,
    unit_price_raw: Any,
    line_total_raw: Any,
    discount_raw: Any = None,
    discount_percentage_raw: Any = None
) -> Tuple[Optional[str], bool]:
    """
    Deterministic unit_price resolution per Step 6 Requirement 6:
    - If unit_price_raw is present:
        - Check if unit_price_raw equals line_total_raw while quantity > 1 (e.g. batch/lot price per 100 or total copied into unit price).
          In this case, the true single-unit net price is line_total / quantity (derived=True).
        - Otherwise, normalise and keep derived=False.
    - If unit_price_raw is absent/null:
        - ONLY derive if BOTH printed quantity AND line_total are present, non-zero, and unambiguous.
        - If either is missing, do NOT derive: return (None, False).
    """
    d_total, total_str, amb_total = normalise_financial_amount(line_total_raw)
    d_qty, qty_str, amb_qty = normalise_financial_amount(quantity_raw)

    # 1. If explicitly printed
    if unit_price_raw is not None and str(unit_price_raw).strip() != "":
        d_price, price_str, amb = normalise_financial_amount(unit_price_raw)
        # Lot / batch pricing check: if unit_price == total and quantity > 1
        if d_price is not None and d_total is not None and d_qty is not None and d_qty > 1:
            if abs(d_price - d_total) < Decimal("0.01"):
                try:
                    derived = d_total / d_qty
                    derived_str = f"{derived:.4f}".rstrip("0").rstrip(".") if "." in f"{derived:.4f}" else f"{derived}"
                    return derived_str, True
                except Exception:
                    pass

        # Check if d_price is already consistent with d_total (taking any line discounts into account)
        d_disc_pct, _, _ = normalise_financial_amount(discount_percentage_raw)
        d_disc_amt, _, _ = normalise_financial_amount(discount_raw)
        expected_total = d_qty * d_price if (d_qty is not None and d_price is not None) else None
        if expected_total is not None and d_disc_pct is not None and d_disc_pct > 0:
            expected_total = expected_total * (Decimal("1") - d_disc_pct / Decimal("100"))
        elif expected_total is not None and d_disc_amt is not None and d_disc_amt > 0:
            expected_total = expected_total - d_disc_amt

        if expected_total is not None and d_total is not None and abs(expected_total - d_total) <= Decimal("0.05"):
            if d_price is not None:
                return price_str, False
            return str(unit_price_raw), False

        # Column leakage check: if d_qty * d_price differs wildly from line total (e.g. section subtotal leaked into unit price)
        if d_price is not None and d_total is not None and d_qty is not None and d_qty != 0:
            if abs(d_qty * d_price - d_total) > Decimal("0.05"):
                try:
                    derived = d_total / d_qty
                    derived_str = f"{derived:.4f}".rstrip("0").rstrip(".") if "." in f"{derived:.4f}" else f"{derived}"
                    return derived_str, True
                except Exception:
                    pass
        if d_price is not None:
            return price_str, False
        return str(unit_price_raw), False

    # 2. If unit_price_raw is absent/null:
    # Per Step 6 Requirement 6: ONLY derive if BOTH quantity AND line_total are present and valid
    if quantity_raw is None or str(quantity_raw).strip() == "" or d_qty is None or d_qty == 0 or amb_qty:
        return None, False
    if line_total_raw is None or str(line_total_raw).strip() == "" or d_total is None or amb_total:
        return None, False

    try:
        derived = d_total / d_qty
        derived_str = f"{derived:.4f}".rstrip("0").rstrip(".") if "." in f"{derived:.4f}" else f"{derived}"
        return derived_str, True
    except Exception:
        return None, False
