"""
src/bookable/verify.py

Responsibility: Call erp_book() and compare the recomputed gross to the document's stated gross.

Given a fully assembled autodraft dict, this module:
  1. Calls erp_book(payable) from erp.py (the sealed oracle).
  2. Compares will_book_gross to the gross_total extracted from the document.
  3. Returns a verification result: PASS (match to the cent) or FAIL (with delta).

On FAIL, it does NOT attempt to auto-fix values to make the number balance --
that would violate Rule 1 (values must be grounded) and Rule 3 (corrections must
survive being wrong). A failed verification is reported as-is; root cause
investigation is left to a human or a future diagnostic layer.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict
from erp import erp_book, num, round2


def verify_payable_erp(payable: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call erp_book() on a schema-compliant payable dict and compare with stated gross_total.
    Returns:
      {
        "will_book_gross": float,
        "currency": str,
        "stated_gross": float,
        "delta": float,
        "is_match": bool
      }
    """
    stated_gross_str = payable.get("gross_total") or ""
    stated_gross = round2(num(stated_gross_str))

    erp_result = erp_book(payable)
    will_book_gross = erp_result.get("will_book_gross", 0.0)
    currency = erp_result.get("currency", "")

    # Calculate delta: erp_gross - stated_gross
    delta = round2(will_book_gross - stated_gross)
    is_match = abs(delta) < 0.005

    return {
        "will_book_gross": will_book_gross,
        "currency": currency,
        "stated_gross": stated_gross,
        "delta": delta,
        "is_match": is_match
    }
