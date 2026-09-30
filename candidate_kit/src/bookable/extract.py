"""
src/bookable/extract.py

Responsibility: Pull raw field values out of a classified payable document segment.

Given rendered page images (and any embedded text layer) for ONE payable segment,
calls Gemini Vision and returns a raw dict of field values exactly as they appear
on the document -- no master-data resolution, no ERP computation here.

Key design rules:
  - Never invent or calculate values (README Rule 1).
  - For uncertain / illegible values: mark as null, not guessed.
  - Locale numeric strings (e.g. "1.234,56") are flagged with the raw string preserved.
    Normalisation to dot-decimal happens in ground.py, not here.
  - Page/source evidence is stored alongside every extracted group.
  - Declined segments are passed through with their Step 4A reason intact.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymupdf as fitz
from PIL import Image
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# Gemini retry helper (shared pattern with classify.py)
# ──────────────────────────────────────────────────────────────────────────────

def _call_gemini_retry(
    client: genai.Client,
    model: str,
    contents: list,
    config: Optional[types.GenerateContentConfig] = None,
    max_retries: int = 4,
) -> Any:
    """Call Gemini with exponential backoff on 503/429 transient errors."""
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=model, contents=contents, config=config
            )
        except (ServerError, ClientError) as exc:
            msg = str(exc)
            if any(tok in msg for tok in ("503", "429", "UNAVAILABLE", "TEMPORARY")):
                wait = (attempt + 1) * 4
                print(
                    f"[extract] Gemini transient error (attempt {attempt+1}/{max_retries}). "
                    f"Retrying in {wait}s…"
                )
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(
        f"[extract] Gemini API failed after {max_retries} retries."
    )


# ──────────────────────────────────────────────────────────────────────────────
# Locale-aware numeric normalisation (ONLY for display; ground.py is authoritative)
# ──────────────────────────────────────────────────────────────────────────────

_LOCALE_COMMA = re.compile(r"^-?[\d]{1,3}(?:\.\d{3})*,\d{1,4}$")   # e.g. 1.234,56
_PLAIN_COMMA  = re.compile(r"^-?\d+,\d{1,4}$")                       # e.g. 123,36


def normalise_number(raw: str) -> Tuple[str, str]:
    """
    Return (normalised_dot_decimal, raw_original).
    Two-pass:
      1. Strip currency symbols and whitespace.
      2. Detect European locale (comma-decimal / period-thousands) and convert.
    Never raises -- returns ("", raw) on total failure.
    """
    if not raw or not isinstance(raw, str):
        return ("", str(raw) if raw is not None else "")

    stripped = raw.strip().lstrip("€$£¥₹ \t").rstrip()
    # Remove trailing % if present
    stripped = stripped.rstrip("%").strip()

    if _LOCALE_COMMA.match(stripped):
        # "1.234,56" → "1234.56"
        normalised = stripped.replace(".", "").replace(",", ".")
        return (normalised, raw)

    if _PLAIN_COMMA.match(stripped):
        # "123,36" → "123.36"
        normalised = stripped.replace(",", ".")
        return (normalised, raw)

    # Already dot-decimal or integer
    cleaned = stripped.replace(",", "")  # remove thousands separators like "17,657.53"
    return (cleaned, raw)


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
# Extraction prompt
# ──────────────────────────────────────────────────────────────────────────────

_EXTRACTION_PROMPT_TEMPLATE = """\
You are an expert Accounts Payable (AP) document extraction engine.
Your job: extract ALL fields from the document image(s) for ONE payable segment.

STRICT RULES — these are graded:
1. Extract ONLY values that are EXPLICITLY VISIBLE on the page(s). Never invent or calculate.
2. For numbers: extract the RAW string exactly as printed (e.g. "-400,00" not "-400.00").
   Use dot-decimal ONLY if the document uses it. Preserve currency symbols where printed.
3. Mark a field null if it is absent, illegible, or ambiguous. Do NOT guess.
4. For credit memos: the invoice_type must be "CREDIT_MEMO". Line item amounts should be
   extracted as printed (possibly negative on the page). Note: ERP expects positive magnitudes
   for credit memo submission — mark raw_credit=true if values are negative on the page.
5. A tax on a LINE stays on that line in line_items[].taxes[]. A single header-level tax
   belongs in header_taxes[]. Do not migrate taxes from where the document places them.
6. For withholding tax: it appears as a DEDUCTION. Extract as a negative tax_amount.
7. For each line item: if unit_price is not explicitly printed but total is, DO NOT compute
   unit_price. Leave it null and note the evidence.
8. The "total" on a line is the line extension as printed. Do not recalculate.
9. Segment context: <<<SEGMENT_CONTEXT>>>

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
  "gross_total_raw": "string as printed e.g. '17,657.53' or null",
  "subtotal_raw": "string as printed or null",
  "total_tax_raw": "string as printed or null",
  "discount_amount_raw": "header-level discount amount as printed or null",
  "freight_charges_raw": "string or null",
  "insurance_charges_raw": "string or null",
  "extra_charges_raw": "string or null",
  "excise_duties_raw": "string or null",
  "header_taxes": [
    {
      "tax_name": "string e.g. 'VAT' 'KM' 'IVA'",
      "tax_rate_raw": "string as printed e.g. '19%' or '19' or null",
      "tax_amount_raw": "string as printed e.g. '76,00' or null. Negative for withholding.",
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
    client: Optional[genai.Client] = None,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Extract fields from a single page-segment of a PDF.

    Args:
        pdf_path:   Path to the PDF file.
        segment:    One entry from Step 4A page_segmentation[].
                    Must have keys: segment_id, type, pages, description.
        client:     google.genai.Client (created if None).
        model_name: Gemini model name from .env if not provided.

    Returns:
        Dict with all extracted fields plus metadata keys starting with '_'.
        '_decision' is carried from segment type for downstream use.
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"[extract] PDF not found: {path}")

    if client is None:
        client = genai.Client()
    if model_name is None:
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

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

    contents: list = [prompt]
    for p_info, img in zip(page_data, images):
        contents.append(f"--- PAGE {p_info['page_num']} ---")
        if p_info["has_text"]:
            # Provide text layer as a hint — helps with locale parsing
            contents.append(
                f"Embedded text layer snippet (use as reading aid, not authoritative):\n"
                f"{p_info['text'][:1000]}"
            )
        contents.append(img)

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.0,  # deterministic extraction
    )

    # ── Call Gemini ──
    response = _call_gemini_retry(client, model_name, contents, config=config)

    # ── Parse response ──
    try:
        extracted = json.loads(response.text)
    except json.JSONDecodeError as exc:
        return {
            "_segment_id": seg_id,
            "_decision": "PARSE_ERROR",
            "_segment_type": seg_type,
            "_source_pages": pages,
            "_description": description,
            "_extraction_notes": f"JSON parse failed: {exc}. Raw: {response.text[:400]}",
            "invoice_type": seg_type,
        }

    # ── Enrich with segment metadata ──
    extracted["_segment_id"] = seg_id
    extracted["_segment_type"] = seg_type
    extracted["_source_pages"] = pages
    extracted["_description"] = description
    extracted["_decision"] = "PROCESS"
    extracted["_pdf_file"] = path.name

    # ── Normalise all *_raw numeric strings ──
    #    We store both the normalised dot-decimal and the original raw.
    _apply_normalisation(extracted)

    return extracted


def _apply_normalisation(d: Dict[str, Any]) -> None:
    """
    Walk the extraction dict and for every key ending in '_raw' that holds a string,
    add a companion key ending in '_norm' with the dot-decimal normalised value.
    Operates in-place.
    """
    for key in list(d.keys()):
        if key.endswith("_raw") and isinstance(d[key], str):
            norm, _ = normalise_number(d[key]) if d[key] else ("", "")
            d[key.replace("_raw", "_norm")] = norm

    # Recurse into supplier/buyer dicts
    for sub_key in ("supplier", "buyer"):
        if isinstance(d.get(sub_key), dict):
            _apply_normalisation(d[sub_key])

    # Recurse into header_taxes list
    for t in d.get("header_taxes") or []:
        if isinstance(t, dict):
            _apply_normalisation(t)

    # Recurse into line_items list
    for li in d.get("line_items") or []:
        if isinstance(li, dict):
            _apply_normalisation(li)
            for t in li.get("taxes") or []:
                if isinstance(t, dict):
                    _apply_normalisation(t)


# ──────────────────────────────────────────────────────────────────────────────
# Multi-segment driver: extract all segments for one PDF classification result
# ──────────────────────────────────────────────────────────────────────────────

def extract_pdf(
    pdf_path: str | Path,
    classification: Dict[str, Any],
    client: Optional[genai.Client] = None,
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Given the Step 4A classification result for a single PDF, extract all payable segments.

    Returns output-contract shaped dict:
    {
        "file":     "X.pdf",
        "decision": "PROCESS" | "DECLINE" | "SPLIT" | "MANUAL_REVIEW",
        "payables": [ <extracted_payable>, ... ],
        "declined": [ { "doc_type": ..., "reason": ... } ],
        "_classification": { ... }   # full Step 4A result preserved
    }
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
        extraction = extract_segment(path, seg, client=client, model_name=model_name)

        if seg_type == "DECLINED_DOC":
            result["declined"].append({
                "doc_type": seg.get("type", "UNKNOWN"),
                "reason": seg.get("description", ""),
                "_evidence": extraction.get("_extraction_notes", ""),
            })
        else:
            result["payables"].append(extraction)

    return result
