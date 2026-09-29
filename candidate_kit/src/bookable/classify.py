"""
classify.py

Responsibility: Decide what a rendered document IS before attempting field extraction.

Given the rendered pages of one PDF, determine:
  - How many bookable payables the document contains (0, 1, or more).
  - The type of each payable: INVOICE or CREDIT_MEMO.
  - Which pages (or page ranges) belong to each payable.
  - What to do with pages that are NOT payables (declines with a reason).

This runs before extract.py so that extraction only runs on pages we believe are payable.
"""
