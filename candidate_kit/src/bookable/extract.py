"""
extract.py

Responsibility: Pull raw field values out of a classified payable document.

Given the rendered pages for one payable (text or images), call the LLM/vision model
and return a raw dictionary of field values exactly as they appear on the document:
  - supplier name, VAT ID, address
  - invoice number, date, due date, currency
  - line items (description, qty, unit price, printed total, discount, tax rate/amount)
  - header-level charges (freight, insurance, extra, excise)
  - header-level taxes (name, rate, amount)
  - gross total, subtotal, total tax as printed

No master-data resolution, no ERP computation here -- only what the page says.
"""
