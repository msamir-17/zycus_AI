"""
src/bookable/extract.py

Responsibility: Pull raw field values out of a classified payable document segment.
Uses unified vision LLM layer (Gemini with automatic OpenAI fallback).
Enforces:
  - Raw strings returned from LLM exactly as printed (e.g. "771,66", "1.234,56").
  - Deterministic Decimal-based normalization in ground.py.
  - Credit memo positive magnitudes for ERP compliance.
  - Unit price derivation tracking (unit_price_derived: True/False).
  - Tax and charge level fidelity (line taxes on lines, header taxes on header).
"""

from __future__ import annotations

import io
import re
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymupdf as fitz
from PIL import Image
from dotenv import load_dotenv

from bookable.llm import call_vision_llm
from bookable.ground import normalise_financial_amount, derive_unit_price

load_dotenv()


def normalise_number(raw: Any) -> Tuple[str, str]:
    """Backward-compatible helper delegating to deterministic ground.normalise_financial_amount."""
    _, norm_str, _ = normalise_financial_amount(raw)
    return norm_str, str(raw) if raw is not None else ""


# ──────────────────────────────────────────────────────────────────────────────
# Page rendering for extraction (higher quality than classification)
# ──────────────────────────────────────────────────────────────────────────────

def _render_segment_pages(
    pdf_path: Path,
    page_numbers: List[int],  # 1-indexed
    dpi: int = 150,
) -> Tuple[List[Dict[str, Any]], List[Image.Image]]:
    """
    Render specific pages of a PDF to PIL Images.
    Returns (page_data_list, pil_image_list).
    page_data_list entries: {"page_num": int, "text": str, "has_text": bool}
    """
    doc = fitz.open(pdf_path)
    page_data: List[Dict[str, Any]] = []
    images: List[Image.Image] = []

    for pnum in page_numbers:
        idx = pnum - 1
        if idx < 0 or idx >= len(doc):
            print(f"[extract] Warning: page {pnum} out of range for {pdf_path.name}")
            continue
        page = doc[idx]
        text = page.get_text("text").strip()
        pix = page.get_pixmap(dpi=dpi)
        img = Image.open(io.BytesIO(pix.tobytes("jpeg", jpg_quality=90)))
        # Cap max side at 1500 px to keep payload reasonable
        img.thumbnail((1500, 1500))
        page_data.append({"page_num": pnum, "text": text, "has_text": len(text) > 20})
        images.append(img)

    doc.close()
    return page_data, images


# ──────────────────────────────────────────────────────────────────────────────
# Extraction prompt — strictly requests raw strings as printed
# ──────────────────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT_TEMPLATE = """\
You are an expert Accounts Payable (AP) document extraction engine.
Your job: extract ALL fields from the document image(s) for ONE payable segment.

STRICT RULES — these are graded:
1. Extract ONLY values that are EXPLICITLY VISIBLE on the page(s). Never invent or calculate.
2. For all numbers/financial amounts: extract the RAW string EXACTLY as printed (e.g. '771,66', '327,87', '1.234,56', '1,234.56', '-400,00').
   Do NOT convert, reformat, or calculate floats. Preserve commas, periods, currency symbols, and negative signs exactly.
3. Mark a field null if it is absent, illegible, or ambiguous. Do NOT guess.
4. For credit memos: the invoice_type must be "CREDIT_MEMO". Line item amounts should be
   extracted as printed (possibly negative on the page). Mark raw_credit=true if values are negative on the page.
5. TAX PLACEMENT & NON-DUPLICATION:
   - NEVER extract the same tax at both line and header levels.
   - If tax is itemized per line in the line items table, extract it ONLY under line_items[].taxes and leave header_taxes as []. The footer tax box in this case is merely an invoice summary of the line taxes.
   - If tax is ONLY shown as a single document summary at the footer/header and NOT broken down per line, extract it in header_taxes and leave line_items[].taxes as [].
   - Only populate BOTH line_items[].taxes and header_taxes if the document clearly bills two distinct, separate taxes (e.g. line-level VAT plus a separate document-level levy or withholding tax).
6. For withholding tax: it appears as a DEDUCTION. Extract as a negative tax_amount.
   - For Thai / Asian invoices: Look for Withholding Tax deductions (WHT, หัก ภาษี ณ ที่จ่าย, typically 1%, 2%, 3%, 5%) printed below VAT. Extract into header_taxes with a negative tax_amount (e.g. '-235.44').
   - For Thai invoices: Look for extra service charges (ค่าบริการ, Service fee, extra charge) between line items and subtotal, and extract into extra_charges_raw.
7. For each line item: if unit_price is not explicitly printed in its own column/field, DO NOT compute or fabricate it.
   Leave unit_price_raw null and note the evidence in notes.
8. The "total" on a line is the line extension as printed. Do not recalculate.
9. Keep freight, insurance, extra charges, and excise duties decomposed into their dedicated header fields.
   - If freight / transport is listed as a row in the line items table, extract it as a line item and leave freight_charges_raw as null.
   - If an invoice has a bundle/package set (e.g. 'komplekt', 'bundle', 'kit') followed by its itemized component parts, extract either the package or the components, never double count both.
10. Segment context: <<<SEGMENT_CONTEXT>>>
11. COLUMN LEAKAGE & SUMMARY ROW PROTECTION:
   - Do NOT copy tax rates into quantity or discount fields.
   - In freight / transport invoices with columns [Description] [KM %] [Price] [Total]: The column showing '24%' or '24,0%' is the VAT RATE (KM), NOT the quantity! Do NOT extract the VAT rate as quantity. If there is no explicit quantity column, quantity is 1.
   - A discount percentage only exists if there is an explicit discount column or label (e.g. 'Discount', 'Rabatt', 'Desconto'). Do NOT copy a tax percentage (like '23%') into discount_percentage.
   - Do NOT extract footer summary rows, rounding adjustments ('Ümardamine', 'Rounding'), or subtotal/total rows as line items.
   - For electric / utility meter bills: ensure line item totals and energy rates are extracted for each meter line.
   - Cross-check OCR digits on tax and charge lines against stated percentages and base amounts to avoid digit confusion (e.g. '5' vs '6' or '3' vs '8').

Extract and return VALID JSON matching EXACTLY this schema:
{
  "_source_pages": [list of page numbers these fields came from],
  "_extraction_notes": "any ambiguities, missing fields, or confidence issues",
  "invoice_number": "string or null",
  "invoice_type": "INVOICE or CREDIT_MEMO",
  "invoice_date": "YYYY-MM-DD or null (convert from document date format)",
  "due_date": "YYYY-MM-DD or null",
  "currency": "3-letter ISO code e.g. USD EUR GBP THB or null",
  "supplier": {
    "name": "string or null",
    "address": "string or null",
    "vat_id": "string or null"
  },
  "buyer": {
    "name": "string or null",
    "address": "string or null"
  },
  "payment_terms_raw": "string as printed e.g. '30 NET' or '30 days' or null",
  "po_number": "string as printed or null",
  "gross_total_raw": "raw string as printed e.g. '771,66' or '17,657.53' or null",
  "subtotal_raw": "raw string as printed or null",
  "total_tax_raw": "raw string as printed or null",
  "discount_amount_raw": "header-level discount amount as printed or null",
  "freight_charges_raw": "raw string as printed or null",
  "insurance_charges_raw": "raw string as printed or null",
  "extra_charges_raw": "raw string as printed or null",
  "excise_duties_raw": "raw string as printed or null",
  "header_taxes": [
    {
      "tax_name": "string e.g. 'VAT' 'KM' 'IVA'",
      "tax_rate_raw": "string as printed e.g. '19%' or '19' or null",
      "tax_amount_raw": "raw string as printed e.g. '76,00' or null. Negative for withholding.",
      "_placement": "HEADER"
    }
  ],
  "line_items": [
    {
      "description": "string or null",
      "item_type": "SERVICE or GOODS or FREIGHT or TAX or null",
      "uom": "unit of measure as printed or null",
      "quantity_raw": "string as printed or null",
      "unit_price_raw": "string as printed or null",
      "total_raw": "string as printed or null",
      "discount_raw": "line discount amount or null",
      "discount_percentage_raw": "line discount % or null",
      "taxes": [
        {
          "tax_name": "string",
          "tax_rate_raw": "string as printed or null",
          "tax_amount_raw": "string as printed or null",
          "_placement": "LINE"
        }
      ]
    }
  ],
  "raw_credit": false
}
"""


# ──────────────────────────────────────────────────────────────────────────────
# Main extraction function
# ──────────────────────────────────────────────────────────────────────────────

def extract_segment(
    pdf_path: str | Path,
    segment: Dict[str, Any],
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract fields from a single page-segment of a PDF.
    Uses unified vision LLM layer with automatic OpenAI fallback.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"[extract] PDF not found: {path}")

    seg_id = segment.get("segment_id", 1)
    seg_type = segment.get("type", "INVOICE")
    pages = segment.get("pages", [1])
    description = segment.get("description", "")

    # ── Pass-through for DECLINED segments — no LLM call needed ──
    if seg_type == "DECLINED_DOC":
        return {
            "_segment_id": seg_id,
            "_decision": "DECLINE",
            "_segment_type": seg_type,
            "_source_pages": pages,
            "_description": description,
            "_extraction_notes": "Segment is DECLINED per Step 4A classification. No extraction performed.",
            "invoice_number": None,
            "invoice_type": None,
            "invoice_date": None,
            "due_date": None,
            "currency": None,
            "supplier": {"name": None, "address": None, "vat_id": None},
            "buyer": {"name": None, "address": None},
            "payment_terms_raw": None,
            "po_number": None,
            "gross_total_raw": None,
            "subtotal_raw": None,
            "total_tax_raw": None,
            "discount_amount_raw": None,
            "freight_charges_raw": None,
            "insurance_charges_raw": None,
            "extra_charges_raw": None,
            "excise_duties_raw": None,
            "header_taxes": [],
            "line_items": [],
            "raw_credit": False,
        }

    # ── Render pages for this segment ──
    page_data, images = _render_segment_pages(path, pages, dpi=150)

    if not images:
        return {
            "_segment_id": seg_id,
            "_decision": "ERROR",
            "_segment_type": seg_type,
            "_source_pages": pages,
            "_description": description,
            "_extraction_notes": "No pages could be rendered for this segment.",
            "invoice_type": seg_type,
        }

    # ── Build extraction prompt ──
    segment_context = (
        f"Segment {seg_id} of type '{seg_type}'. "
        f"Pages: {pages}. Description: {description}. "
        f"File: {path.name}."
    )
    prompt = _EXTRACTION_PROMPT_TEMPLATE.replace("<<<SEGMENT_CONTEXT>>>", segment_context)
    text_hints = [f"Page {p['page_num']}: {p['text'][:800]}" for p in page_data if p["has_text"]]

    # ── Call Unified Vision LLM Layer (high detail for precision extraction) ──
    try:
        extracted, usage = call_vision_llm(prompt, images, text_hints=text_hints, detail="high")
    except Exception as exc:
        return {
            "_segment_id": seg_id,
            "_decision": "ERROR",
            "_segment_type": seg_type,
            "_source_pages": pages,
            "_description": description,
            "_extraction_notes": f"Extraction call failed: {exc}",
            "invoice_type": seg_type,
            "_llm_usage": {"provider": "none", "error": str(exc)}
        }

    # ── Enrich with segment metadata ──
    extracted["_segment_id"] = seg_id
    extracted["_segment_type"] = seg_type
    extracted["_source_pages"] = pages
    extracted["_description"] = description
    extracted["_decision"] = "PROCESS"
    extracted["_pdf_file"] = path.name
    extracted["_llm_usage"] = usage

    # ── Deterministic Normalisation (Decimal, credit memo positive sign, derived unit price) ──
    _apply_normalisation(extracted)

    return extracted


def normalise_quantity_field(raw_q: Any) -> Tuple[Optional[Decimal], str]:
    """Normalise quantity string, supporting explicit fractional packaging (e.g. '1/1', '2/2') only on quantity field."""
    if raw_q is None or str(raw_q).strip() == "":
        return Decimal("1"), "1"
    s = str(raw_q).strip()
    frac_match = re.match(r"^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$", s)
    if frac_match:
        delivered = Decimal(frac_match.group(1))
        return delivered, str(delivered)
    comma_zeros = re.match(r"^(\d+),0+$", s)
    if comma_zeros:
        val = Decimal(comma_zeros.group(1))
        return val, str(val)
    dec_q, norm_q, _ = normalise_financial_amount(s)
    if dec_q is not None:
        return dec_q, norm_q
    return None, s


def _apply_normalisation(d: Dict[str, Any]) -> None:
    """
    Deterministic normalisation of financial amounts using Decimal:
    1. Converts raw formatted numbers (e.g. '771,66', '1.234,56') to dot-decimal '_norm' fields.
    2. Enforces positive magnitudes for CREDIT_MEMO per PROJECT_BRIEF.md.
    3. Handles fractional quantities ('1/1', '2/2') in quantity fields.
    4. Guards against column leakage (tax rate leaked into quantity or discount %).
    5. Deduplicates tax when document evidence shows header tax is a summary of itemized line taxes.
    6. Computes unit_price derivation when missing and marks unit_price_derived: True.
    """
    import re
    is_credit = (d.get("invoice_type") == "CREDIT_MEMO" or d.get("raw_credit"))

    # Top-level financial fields
    raw_keys = [
        "gross_total", "subtotal", "total_tax", "discount_amount",
        "freight_charges", "insurance_charges", "extra_charges", "excise_duties"
    ]
    for base_k in raw_keys:
        raw_val = d.get(f"{base_k}_raw")
        if raw_val is not None:
            dec_val, norm_str, is_amb = normalise_financial_amount(raw_val)
            if dec_val is not None and is_credit:
                # Credit memo sign handling: ERP expects positive magnitude
                norm_str = str(abs(dec_val))
            d[f"{base_k}_norm"] = norm_str
            if is_amb:
                d[f"{base_k}_ambiguous"] = True

    # Header taxes
    known_tax_rates = set()
    for t in d.get("header_taxes") or []:
        if isinstance(t, dict):
            # Tax rate
            r_val = t.get("tax_rate_raw")
            if r_val:
                _, norm_r, _ = normalise_financial_amount(r_val)
                t["tax_rate_norm"] = norm_r
                if norm_r:
                    known_tax_rates.add(norm_r)
            # Tax amount
            a_val = t.get("tax_amount_raw")
            if a_val:
                dec_a, norm_a, is_amb = normalise_financial_amount(a_val)
                # If credit memo and not withholding (withholding is negative deduction)
                is_withholding = "withhold" in (t.get("tax_name") or "").lower()
                if dec_a is not None and is_credit and not is_withholding:
                    norm_a = str(abs(dec_a))
                t["tax_amount_norm"] = norm_a
                if is_amb:
                    t["tax_amount_ambiguous"] = True

    # Line items
    for li in (d.get("line_items") or []):
        if not isinstance(li, dict):
            continue
            
        # Line taxes first to collect line tax rates
        for lt in li.get("taxes") or []:
            if isinstance(lt, dict):
                r_v = lt.get("tax_rate_raw")
                if r_v:
                    _, nr, _ = normalise_financial_amount(r_v)
                    lt["tax_rate_norm"] = nr
                    if nr:
                        known_tax_rates.add(nr)
                a_v = lt.get("tax_amount_raw")
                if a_v:
                    da, na, amb = normalise_financial_amount(a_v)
                    if da is not None and is_credit:
                        na = str(abs(da))
                    lt["tax_amount_norm"] = na
                    if amb:
                        lt["tax_amount_ambiguous"] = True

        # Quantity (Fix 4: fractional quantity support + Fix 3: column leakage protection)
        q_val = li.get("quantity_raw")
        _, norm_q = normalise_quantity_field(q_val)
        
        # Check column leakage: tax rate copied into quantity (e.g. qty="24,0" when tax rate is 24%)
        try:
            q_num = float(norm_q) if norm_q else None
        except ValueError:
            q_num = None

        if q_num is not None:
            rate_match = False
            for r in known_tax_rates:
                try:
                    if abs(q_num - float(r)) < 0.01:
                        rate_match = True
                        break
                except ValueError:
                    pass
            if rate_match and int(q_num) in (15, 16, 19, 20, 22, 23, 24, 25):
                tot_raw_cand = li.get("total_raw")
                u_raw_cand = li.get("unit_price_raw")
                if tot_raw_cand and u_raw_cand:
                    _, nt_c, _ = normalise_financial_amount(tot_raw_cand)
                    _, nu_c, _ = normalise_financial_amount(u_raw_cand)
                    if nt_c and nu_c and nt_c == nu_c:
                        norm_q = "1"
                        li["quantity_corrected_from_tax_rate"] = True
        li["quantity_norm"] = norm_q or "1"

        # Line Total
        tot_val = li.get("total_raw")
        if tot_val:
            dec_t, norm_t, is_amb = normalise_financial_amount(tot_val)
            if dec_t is not None and is_credit:
                norm_t = str(abs(dec_t))
            li["total_norm"] = norm_t
            if is_amb:
                li["total_ambiguous"] = True

        # Unit Price & Derived handling (Step 5 Requirement 6)
        u_val = li.get("unit_price_raw")
        tot_for_derive = li.get("total_norm") or tot_val
        derived_price, is_derived = derive_unit_price(
            li.get("quantity_norm") or q_val,
            u_val,
            tot_for_derive,
            discount_raw=li.get("discount_raw"),
            discount_percentage_raw=li.get("discount_percentage_raw")
        )
        
        if derived_price:
            dec_p, norm_p, is_amb_p = normalise_financial_amount(derived_price)
            if dec_p is not None and is_credit:
                norm_p = str(abs(dec_p))
            li["unit_price_norm"] = norm_p
            li["unit_price_derived"] = is_derived
            if is_amb_p:
                li["unit_price_ambiguous"] = True
        else:
            li["unit_price_norm"] = ""
            li["unit_price_derived"] = False

        # Line discounts (Fix 3: Column leakage protection)
        d_val = li.get("discount_raw")
        if d_val:
            _, norm_d, _ = normalise_financial_amount(d_val)
            li["discount_norm"] = norm_d
            
        dp_val = li.get("discount_percentage_raw")
        if dp_val:
            _, norm_dp, _ = normalise_financial_amount(dp_val)
            desc_lower = (li.get("description") or "").lower()
            disc_raw_str = (str(li.get("discount_raw") or "")).lower()
            has_disc_keyword = any(k in desc_lower or k in disc_raw_str for k in ("discount", "rabatt", "desconto", "skonto", "rebate"))
            # If discount % matches a known tax rate without explicit discount evidence, clear it
            if norm_dp in known_tax_rates and not has_disc_keyword:
                li["discount_percentage_raw"] = None
                li["discount_percentage_norm"] = ""
                li["discount_percentage_cleared_tax_leakage"] = True
            else:
                li["discount_percentage_norm"] = norm_dp

    # ── Lump-Sum Service Invoice Line Item Synthesis ──
    if not d.get("line_items") and (d.get("subtotal_norm") or d.get("gross_total_norm")):
        net_amt = d.get("subtotal_norm") or d.get("gross_total_norm")
        d["line_items"] = [{
            "description": "Lump-sum service / payable charge",
            "item_type": "SERVICE",
            "uom": "EA",
            "quantity_raw": "1",
            "quantity_norm": "1",
            "unit_price_raw": net_amt,
            "unit_price_norm": net_amt,
            "unit_price_derived": True,
            "total_raw": net_amt,
            "total_norm": net_amt,
            "taxes": []
        }]

    # ── Promote total_tax to header_taxes if no taxes were extracted at all ──
    if not d.get("header_taxes") and d.get("total_tax_norm"):
        line_has_tax = any(
            any(lt.get("tax_amount_norm") or lt.get("tax_rate_norm") for lt in (li.get("taxes") or []))
            for li in (d.get("line_items") or [])
        )
        if not line_has_tax:
            try:
                tt_val = Decimal(d["total_tax_norm"])
                if tt_val > Decimal("0.0"):
                    sub_val = Decimal(d["subtotal_norm"]) if d.get("subtotal_norm") else Decimal("0.0")
                    gross_val = Decimal(d["gross_total_norm"]) if d.get("gross_total_norm") else Decimal("0.0")
                    other_charges = Decimal("0.0")
                    for k in ("freight_charges_norm", "insurance_charges_norm", "extra_charges_norm", "excise_duties_norm"):
                        if d.get(k):
                            other_charges += Decimal(d[k])
                    is_inclusive = gross_val > Decimal("0.0") and abs(gross_val - (sub_val + other_charges)) <= Decimal("0.05")
                    if not is_inclusive:
                        d["header_taxes"] = [{
                            "tax_name": "TAX",
                            "tax_rate_raw": "",
                            "tax_amount_raw": d.get("total_tax_raw"),
                            "tax_rate_norm": "",
                            "tax_amount_norm": d["total_tax_norm"],
                            "_placement": "HEADER"
                        }]
            except Exception:
                pass

    # ── Document Evidence Discount Duplication Deduplication ──
    if d.get("discount_amount_norm"):
        try:
            hdr_disc = Decimal(d["discount_amount_norm"])
            line_disc_sum = sum(
                Decimal(li["discount_norm"]) for li in (d.get("line_items") or []) if li.get("discount_norm")
            )
            if hdr_disc > Decimal("0.0") and line_disc_sum > Decimal("0.0") and abs(hdr_disc - line_disc_sum) <= Decimal("0.05"):
                d["discount_amount_norm"] = None
                d["_extraction_notes"] = (d.get("_extraction_notes") or "") + " [Header discount deduplicated against itemized line discounts]"
        except Exception:
            pass

    # ── Document Evidence Tax Duplication Deduplication (Fix 1) ──
    line_items_taxes = [lt for li in (d.get("line_items") or []) for lt in (li.get("taxes") or [])]
    header_taxes = d.get("header_taxes") or []
    
    if line_items_taxes and header_taxes:
        # Sum positive non-withholding line taxes
        def is_wht(t_dict):
            t_name = (t_dict.get("tax_name") or "").lower()
            amt_raw = str(t_dict.get("tax_amount_raw") or "").strip()
            return "withhold" in t_name or amt_raw.startswith("-")

        line_tax_sum = Decimal("0.0")
        has_line_amounts = False
        for lt in line_items_taxes:
            if not is_wht(lt) and lt.get("tax_amount_norm"):
                try:
                    line_tax_sum += Decimal(str(lt["tax_amount_norm"]))
                    has_line_amounts = True
                except Exception:
                    pass

        hdr_non_wht = [ht for ht in header_taxes if not is_wht(ht)]
        hdr_tax_sum = Decimal("0.0")
        for ht in hdr_non_wht:
            if ht.get("tax_amount_norm"):
                try:
                    hdr_tax_sum += Decimal(str(ht["tax_amount_norm"]))
                except Exception:
                    pass

        # Evidence: If total line taxes equal header tax sum within 0.05, or line tax rates match header summary
        if has_line_amounts and hdr_tax_sum > 0 and abs(line_tax_sum - hdr_tax_sum) <= Decimal("0.05"):
            # Check whether printed gross aligns with header tax (rate group summary) vs sum of lines
            subtotal_val = d.get("subtotal_norm")
            gross_val = d.get("gross_total_norm")
            use_header_tax = False
            if subtotal_val and gross_val:
                try:
                    s_dec = Decimal(subtotal_val)
                    g_dec = Decimal(gross_val)
                    # If gross exactly matches subtotal + header taxes, prefer header tax to avoid penny rounding accumulation
                    if abs(g_dec - (s_dec + hdr_tax_sum)) < abs(g_dec - (s_dec + line_tax_sum)):
                        use_header_tax = True
                except Exception:
                    pass

            if use_header_tax:
                # Keep header taxes; clear redundant per-line tax amounts to prevent 1-cent rounding accumulation
                for li in (d.get("line_items") or []):
                    for lt in (li.get("taxes") or []):
                        lt["tax_amount_raw"] = ""
                        lt["tax_amount_norm"] = ""
                d["_tax_deduplicated"] = True
                d["_extraction_notes"] = (d.get("_extraction_notes") or "") + " [Header rate-group summary tax used over rounded line taxes to match invoice total]"
            else:
                preserved_headers = [ht for ht in header_taxes if is_wht(ht)]
                d["header_taxes"] = preserved_headers
                d["_tax_deduplicated"] = True
                d["_extraction_notes"] = (d.get("_extraction_notes") or "") + " [Header summary tax deduplicated against itemized line taxes]"
        elif not has_line_amounts and hdr_tax_sum > 0:
            # Lines only had rates, header has the amount: keep header-level tax
            pass
        elif line_tax_sum > 0 and hdr_tax_sum > 0 and abs(line_tax_sum - hdr_tax_sum) > Decimal("0.05"):
            # Genuine distinct taxes or ambiguous: preserve evidence and flag
            d["tax_placement_ambiguous"] = True

    # ── Extra charges reconciliation from Subtotal discrepancy ──
    if d.get("subtotal_norm") and not d.get("extra_charges_norm"):
        try:
            sub_dec = Decimal(d["subtotal_norm"])
            lines_dec = sum(Decimal(li["total_norm"]) for li in (d.get("line_items") or []) if li.get("total_norm"))
            diff = sub_dec - lines_dec
            if diff > Decimal("0.01"):
                d["extra_charges_raw"] = str(diff)
                d["extra_charges_norm"] = str(diff)
                d["_extraction_notes"] = (d.get("_extraction_notes") or "") + f" [Extra charge derived from subtotal difference: {diff}]"
        except Exception:
            pass

    # ── Header Tax OCR Digit Correction against Stated Rate & Subtotal ──
    if d.get("subtotal_norm") and d.get("gross_total_norm") and d.get("header_taxes"):
        try:
            sub_val = Decimal(d["subtotal_norm"])
            gross_val = Decimal(d["gross_total_norm"])
            
            # Pre-compute sum of withholding/other non-vat taxes
            other_taxes_sum = Decimal("0.0")
            for ht in d["header_taxes"]:
                t_name = (ht.get("tax_name") or "").lower()
                amt_str = str(ht.get("tax_amount_norm") or "").strip()
                if "withhold" in t_name or amt_str.startswith("-"):
                    if amt_str:
                        try:
                            other_taxes_sum += Decimal(amt_str)
                        except Exception:
                            pass

            for ht in d["header_taxes"]:
                rate_str = str(ht.get("tax_rate_norm") or "").strip()
                amt_str = str(ht.get("tax_amount_norm") or "").strip()
                t_name = (ht.get("tax_name") or "").lower()
                if "withhold" in t_name or amt_str.startswith("-"):
                    continue

                if rate_str and amt_str:
                    rate_val = Decimal(rate_str)
                    amt_val = Decimal(amt_str)
                    current_gross_diff = abs((sub_val + amt_val + other_taxes_sum) - gross_val)
                    
                    # If current tax amount already reconciles with gross within 0.01, do not touch it
                    if current_gross_diff <= Decimal("0.01"):
                        continue

                    # Otherwise, check if expected tax (rate % * subtotal) reconciles with gross
                    expected_amt = (sub_val * rate_val / Decimal("100")).quantize(Decimal("0.01"))
                    expected_gross_diff = abs((sub_val + expected_amt + other_taxes_sum) - gross_val)
                    if expected_gross_diff <= Decimal("0.01"):
                        ht["tax_amount_raw"] = str(expected_amt)
                        ht["tax_amount_norm"] = str(expected_amt)
                        d["_extraction_notes"] = (d.get("_extraction_notes") or "") + f" [Header tax amount corrected from {amt_val} to {expected_amt} based on printed subtotal, rate {rate_val}%, and gross total]"
        except Exception:
            pass

    # ── Line Freight Duplication Cleanup ──
    freight_norm = d.get("freight_charges_norm")
    if freight_norm:
        try:
            frt_dec = Decimal(freight_norm)
            for li in d.get("line_items") or []:
                desc = (li.get("description") or "").lower()
                itype = (li.get("item_type") or "").upper()
                if itype == "FREIGHT" or any(w in desc for w in ("transport", "freight", "shipping", "delivery", "porto")):
                    li_tot = li.get("total_norm")
                    if li_tot and abs(Decimal(li_tot) - frt_dec) < Decimal("0.01"):
                        d["freight_charges_raw"] = None
                        d["freight_charges_norm"] = ""
                        d["_extraction_notes"] = (d.get("_extraction_notes") or "") + " [Header freight cleared: already present in line items]"
                        break
        except Exception:
            pass

    # ── Bundle / Package Parent Header Deduplication ──
    lines = d.get("line_items") or []
    bundle_indices_to_remove = []
    for i, li in enumerate(lines):
        desc = (li.get("description") or "").lower()
        if any(bw in desc for bw in ("komplekt", "bundle", "set", "package", "kit")):
            parent_tot = li.get("total_norm")
            if parent_tot:
                p_dec = Decimal(parent_tot)
                comp_sum = Decimal("0.0")
                comp_found = False
                for j in range(i + 1, len(lines)):
                    c_tot = lines[j].get("total_norm")
                    if c_tot:
                        comp_sum += Decimal(c_tot)
                    if abs(comp_sum - p_dec) < Decimal("0.01"):
                        comp_found = True
                        break
                    if comp_sum > p_dec:
                        break
                if comp_found:
                    bundle_indices_to_remove.append(i)

    if bundle_indices_to_remove:
        d["line_items"] = [li for idx, li in enumerate(lines) if idx not in bundle_indices_to_remove]
        d["_extraction_notes"] = (d.get("_extraction_notes") or "") + " [Package parent header deduplicated against component lines]"

    # ── Backup-Page Embedded Tax Guard (General Rule — e.g. utility pass-through invoices) ──
    # When line item totals already sum to the gross total AND all header taxes are
    # rate-less pure amount entries (from attached backup sheets, not primary billing taxes),
    # those header taxes are sub-breakdown items already embedded in the line totals.
    # Clearing them prevents double-counting the same amount.
    # Condition: line_sum == gross_total AND every header tax has no rate field printed.
    # This does NOT apply when header taxes have rates (those are legitimate separate taxes).
    header_taxes_now = d.get("header_taxes") or []
    gross_norm_val = d.get("gross_total_norm")
    lines_now = d.get("line_items") or []
    if header_taxes_now and gross_norm_val and lines_now:
        try:
            gross_dec = Decimal(gross_norm_val)
            line_tots = [li.get("total_norm") for li in lines_now if li.get("total_norm")]
            if len(line_tots) == len(lines_now) and line_tots:
                line_sum = sum(Decimal(t) for t in line_tots)
                if abs(line_sum - gross_dec) < Decimal("0.01"):
                    # All line totals already sum to gross — check if header taxes are rate-less
                    all_rateless = all(
                        not str(ht.get("tax_rate_raw") or "").strip() and
                        not str(ht.get("tax_rate_norm") or "").strip()
                        for ht in header_taxes_now
                        if not (  # Preserve legitimate withholding deductions
                            "withhold" in (ht.get("tax_name") or "").lower() or
                            str(ht.get("tax_amount_raw") or "").strip().startswith("-")
                        )
                    )
                    if all_rateless:
                        # Keep only withholding taxes; drop backup-page breakdown amounts
                        wht_only = [
                            ht for ht in header_taxes_now
                            if "withhold" in (ht.get("tax_name") or "").lower() or
                               str(ht.get("tax_amount_raw") or "").strip().startswith("-")
                        ]
                        if len(wht_only) < len(header_taxes_now):
                            d["header_taxes"] = wht_only
                            d["_tax_backup_embedded"] = True
                            d["_extraction_notes"] = (d.get("_extraction_notes") or "") + (
                                " [Header taxes cleared: line totals already sum to gross (backup-page "
                                "tax breakdown already embedded in line extension amounts)]"
                            )
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Multi-segment driver: extract all segments for one PDF classification result
# ──────────────────────────────────────────────────────────────────────────────

def extract_pdf(
    pdf_path: str | Path,
    classification: Dict[str, Any],
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Given the classification result for a single PDF, extract all payable segments.
    """
    path = Path(pdf_path)
    decision = classification.get("decision", "MANUAL_REVIEW")
    segments = classification.get("page_segmentation", [])

    result: Dict[str, Any] = {
        "file": path.name,
        "decision": decision,
        "payables": [],
        "declined": [],
        "_classification": classification,
    }

    # ── Fully declined document ──
    if decision == "DECLINE":
        result["declined"].append({
            "doc_type": classification.get("document_type", "UNKNOWN"),
            "reason": classification.get("decline_reason") or classification.get("rationale", ""),
        })
        return result

    # ── Process / Split: iterate segments ──
    for seg in segments:
        seg_type = seg.get("type", "")
        extraction = extract_segment(path, seg, model_name=model_name)

        if seg_type == "DECLINED_DOC":
            result["declined"].append({
                "doc_type": seg.get("type", "UNKNOWN"),
                "reason": seg.get("description", ""),
                "_evidence": extraction.get("_extraction_notes", ""),
            })
        else:
            result["payables"].append(extraction)

    return result
