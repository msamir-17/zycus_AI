"""
src/bookable/retry.py

Generic Production Targeted Retry Module:
- Executes a single-pass structural diagnostic reread on ERP mismatch.
- Prompt is 100% generic: asks all structural questions in ONE prompt.
- Never passes expected totals, deltas, filenames, or vendor names.
- Uses dedicated cache key: SHA256(PDF_SHA256 + pages + "v1_diagnostic_retry" + model + schema_version).
- Independent evidence grounding via ground.py.
- If ungrounded or persistent mismatch: preserves faithful initial extraction.
"""

from __future__ import annotations

import os
import copy
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from bookable.extract import _render_segment_pages, _apply_normalisation, _EXTRACTION_PROMPT_TEMPLATE
from bookable.llm import call_vision_llm, compute_source_cache_key, SCHEMA_VERSION
from bookable.ground import verify_text_grounding
from bookable.resolve import resolve_master_data, MasterDataIndex
from bookable.verify import verify_payable_erp


_GENERIC_DIAGNOSTIC_RETRY_PROMPT_TEMPLATE = """\
You are an expert Accounts Payable (AP) Document Extraction Engine performing a second-pass structural audit.
A preliminary accounting verification indicates that the extracted itemized breakdown does not reconcile with the document total.

Re-examine the visual document evidence across ALL pages of this payable segment carefully and address ALL of the following structural failure modes:
1. CONTINUATION LINES / MULTI-PAGE TABLES:
   Does the line items table continue across subsequent pages or include multi-tier fee schedules, parking/service tiers, or line extensions that were omitted from page 1?
2. TRADE DISCOUNTS, REBATES & CONCESSIONS:
   Are there uncaptured commercial discounts, trade allowances, promotional rebates, or concessions located between the line items and the total (e.g. 'Discount', 'Rabatt', 'Desconto', 'Remise', 'Concession', 'Desc. Prom.')?
3. TAX PLACEMENT & INCLUSIVE VS EXCLUSIVE PRICING:
   Are individual line item prices tax-inclusive (e.g. 'incl. VAT', 'TTC', 'brutto', '(incl vat)')? If line prices already include tax, ensure the summary tax box is NOT extracted as an additive header tax. Verify that tax is not extracted redundantly at both line and header levels.
4. ADDITIONAL CHARGES & STATUTORY LEVIES:
   Are there separate delivery fees, freight charges, fuel surcharges, service fees, or statutory cascading levies (e.g. NHIL, GETFund, COVID, CST) listed that belong in dedicated charge or tax fields?
5. TABLE COLUMN HEADERS & ALIGNMENT:
   Verify that Quantity and Unit Price columns are not transposed. Check whether unit prices are quoted per unit, per hundred ('%'), or per thousand, and verify that the line total matches the explicit column extension.

EVIDENCE REQUIREMENT:
For every value extracted or corrected, you MUST cite the visual evidence location (exact label, line description, table header, or page section) in '_extraction_notes'.
Extract ONLY values explicitly visible on the page. Never invent or calculate numbers.

Segment context: <<<SEGMENT_CONTEXT>>>

Extract and return VALID JSON matching EXACTLY the standard extraction schema:
{
  "_source_pages": [list of page numbers],
  "_extraction_notes": "CITE VISUAL EVIDENCE LOCATIONS for all fields, especially discounts, taxes, and charges",
  "invoice_number": "string or null",
  "invoice_type": "INVOICE or CREDIT_MEMO",
  "invoice_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "currency": "3-letter ISO code or null",
  "supplier": {"name": "string or null", "address": "string or null", "vat_id": "string or null"},
  "buyer": {"name": "string or null", "address": "string or null"},
  "payment_terms_raw": "string or null",
  "po_number": "string or null",
  "gross_total_raw": "raw string as printed",
  "subtotal_raw": "raw string as printed or null",
  "total_tax_raw": "raw string as printed or null",
  "discount_amount_raw": "header discount amount as printed or null",
  "freight_charges_raw": "raw string as printed or null",
  "insurance_charges_raw": "raw string as printed or null",
  "extra_charges_raw": "raw string as printed or null",
  "excise_duties_raw": "raw string as printed or null",
  "header_taxes": [
    {
      "tax_name": "string",
      "tax_rate_raw": "string or null",
      "tax_amount_raw": "raw string or null",
      "_placement": "HEADER"
    }
  ],
  "line_items": [
    {
      "description": "string or null",
      "item_type": "SERVICE or GOODS or FREIGHT or TAX or null",
      "uom": "string or null",
      "quantity_raw": "string or null",
      "unit_price_raw": "string or null",
      "total_raw": "string or null",
      "discount_raw": "string or null",
      "discount_percentage_raw": "string or null",
      "taxes": [
        {
          "tax_name": "string",
          "tax_rate_raw": "string or null",
          "tax_amount_raw": "string or null",
          "_placement": "LINE"
        }
      ]
    }
  ],
  "raw_credit": false
}
"""


def execute_generic_retry(
    pdf_path: Path | str,
    pages: List[int],
    initial_payable: Dict[str, Any],
    master_index: Optional[MasterDataIndex] = None,
    model_name: Optional[str] = None,
    format_autodraft_payable_fn: Optional[Any] = None
) -> Tuple[Dict[str, Any], bool, str]:
    """
    Execute ONE generic targeted diagnostic reread for an ERP-mismatched payable.
    
    Returns:
      (resolved_payable, is_accepted_match, diagnostic_status)
    """
    path = Path(pdf_path)
    if not path.exists():
        return initial_payable, False, "PDF_NOT_FOUND"

    pdf_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    page_data, images = _render_segment_pages(path, pages, dpi=150)
    if not images:
        return initial_payable, False, "NO_IMAGES_RENDERED"

    seg_id = initial_payable.get("_segment_id", 1)
    seg_type = initial_payable.get("_segment_type", "INVOICE")
    description = initial_payable.get("_description", "")

    # 1. Build GENERIC retry prompt (no target numbers, no delta, no vendor names)
    seg_ctx = f"Segment {seg_id} of type '{seg_type}'. Pages: {pages}. Description: {description}."
    prompt = _GENERIC_DIAGNOSTIC_RETRY_PROMPT_TEMPLATE.replace("<<<SEGMENT_CONTEXT>>>", seg_ctx)
    text_hints = [f"Page {p['page_num']}: {p['text'][:800]}" for p in page_data if p["has_text"]]
    combined_evidence = [p["text"] for p in page_data if p["has_text"]]

    # 2. Dedicated retry cache context
    retry_model = model_name or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
    cache_ctx = {
        "pdf_sha256": pdf_sha256,
        "pages": pages,
        "prompt_version": "v1_diagnostic_retry",
        "model": retry_model,
        "schema_version": SCHEMA_VERSION
    }

    from bookable.llm import CACHE_DIR
    retry_key = compute_source_cache_key(pdf_sha256, pages, "v1_diagnostic_retry", retry_model, SCHEMA_VERSION)
    retry_file = CACHE_DIR / f"{retry_key}.json"
    allow_live = os.getenv("ALLOW_LIVE_CALLS", "1") == "1"
    if not allow_live and not retry_file.exists():
        return initial_payable, False, "UNRESOLVED_AWAITING_RETRY_CACHE"

    try:
        retry_raw, usage = call_vision_llm(
            prompt, images, text_hints=text_hints, detail="high",
            gemini_model=model_name, openai_model=model_name,
            cache_context=cache_ctx
        )
    except Exception as e:
        return initial_payable, False, f"LLM_RETRY_ERROR: {e}"

    # Enrich retry candidate with metadata
    candidate = dict(retry_raw)
    candidate["_segment_id"] = seg_id
    candidate["_segment_type"] = seg_type
    candidate["_source_pages"] = pages
    candidate["_description"] = description
    candidate["_decision"] = "PROCESS"
    candidate["_pdf_file"] = path.name
    candidate["_llm_usage"] = usage
    candidate["_retry_executed"] = True

    # 3. Acceptance & Independent Evidence Grounding Gate
    # Check that changed numeric fields are grounded in document evidence
    fields_to_ground = [
        "gross_total_raw", "subtotal_raw", "total_tax_raw", "discount_amount_raw",
        "freight_charges_raw", "insurance_charges_raw", "extra_charges_raw", "excise_duties_raw"
    ]
    for field in fields_to_ground:
        val = candidate.get(field)
        init_val = initial_payable.get(field)
        if val is not None and str(val).strip() != "":
            # If value changed from initial, it must be independently grounded
            if str(val).strip() != str(init_val or "").strip():
                if combined_evidence and not verify_text_grounding(val, combined_evidence):
                    # Reject ungrounded candidate
                    return initial_payable, False, f"REJECTED_UNGROUNDED_{field}"

    # Also check candidate line items and header taxes
    for t in candidate.get("header_taxes", []):
        t_amt = t.get("tax_amount_raw")
        if t_amt and combined_evidence and not verify_text_grounding(t_amt, combined_evidence):
            return initial_payable, False, "REJECTED_UNGROUNDED_HEADER_TAX"

    # 4. Normalisation & Master Data Resolution on Candidate
    _apply_normalisation(candidate)
    if master_index:
        resolve_master_data(candidate, master_index)

    # 5. ERP Verification of Retry Candidate
    if format_autodraft_payable_fn is not None:
        formatted_candidate = format_autodraft_payable_fn(candidate)
    else:
        from bookable.pipeline import format_autodraft_payable
        formatted_candidate = format_autodraft_payable(candidate)

    v_res = verify_payable_erp(formatted_candidate)
    if v_res.get("is_match"):
        # Match confirmed and independently grounded!
        return candidate, True, "MATCH_AFTER_RETRY"

    # Persistent mismatch: preserve faithful initial extraction
    return initial_payable, False, f"PERSISTENT_MISMATCH_DELTA_{v_res.get('delta')}"
