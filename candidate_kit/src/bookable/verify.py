"""
verify.py

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
