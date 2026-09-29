"""
resolve.py

Responsibility: Match document values against master data and fill ERP codes.

Given grounded raw values, look up:
  - supplier_id    from suppliers.json    (by VAT ID first, then name fuzzy-match)
  - company_code / business_unit_code / location_code  from chart_of_books.json
  - tax_type_code  from tax_master.json   (by country + tax type + rate)
  - payment_term_id from payment_terms.json  (by text alias or derived from dates)
  - po_id          from po_master.json    (exact PO number match only)

Rules:
  - If a match is found, set the code.
  - If no match, leave the code as "" -- an honest blank.
  - Never fabricate a code (Rule 2).
  - Designed for scale: use indexed lookup structures, not brute-force scans.
"""
