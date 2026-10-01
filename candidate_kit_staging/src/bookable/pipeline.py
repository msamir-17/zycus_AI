"""
src/bookable/pipeline.py

Responsibility: Orchestrate the full end-to-end pipeline for one or more PDFs.

Pipeline execution sequence:
  render
  -> classify
  -> extract
  -> deterministic normalization / grounding
  -> resolve master data
  -> verify ERP
  -> format final output

Produces final JSON output adhering strictly to AUTODRAFT_SCHEMA.md:
  {
    "file": "X.pdf",
    "payables": [ <payable>, ... ],
    "declined": [ { "doc_type": "...", "reason": "..." }, ... ]
  }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bookable.classify import classify_pdf
from bookable.extract import extract_pdf
from bookable.resolve import MasterDataIndex, resolve_master_data
from bookable.verify import verify_payable_erp
from bookable.retry import execute_generic_retry


def format_autodraft_payable(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format a raw/normalized internal payable into the exact AUTODRAFT_SCHEMA.md shape.
    Strips all private diagnostic keys (starting with '_') and internal temporary fields.
    """
    supp = d.get("supplier") or {}
    buyer = d.get("buyer") or {}

    # Header taxes formatting
    formatted_taxes: List[Dict[str, str]] = []
    for t in (d.get("taxes") or d.get("header_taxes") or []):
        if not isinstance(t, dict):
            continue
        tax_rate_val = t.get("tax_rate_norm") or t.get("tax_rate_raw") or t.get("tax_rate") or ""
        tax_amt_val = t.get("tax_amount_norm") or t.get("tax_amount_raw") or t.get("tax_amount") or ""
        formatted_taxes.append({
            "tax_type": str(t.get("tax_type") or "VAT").strip(),
            "tax_name": str(t.get("tax_name") or "").strip(),
            "tax_rate": str(tax_rate_val).strip(),
            "tax_amount": str(tax_amt_val).strip(),
            "tax_type_code": str(t.get("tax_type_code") or "").strip(),
        })

    # Line items formatting
    formatted_lines: List[Dict[str, Any]] = []
    for li in (d.get("line_items") or []):
        if not isinstance(li, dict):
            continue

        li_taxes: List[Dict[str, str]] = []
        for lt in (li.get("taxes") or []):
            if not isinstance(lt, dict):
                continue
            lt_rate = lt.get("tax_rate_norm") or lt.get("tax_rate_raw") or lt.get("tax_rate") or ""
            lt_amt = lt.get("tax_amount_norm") or lt.get("tax_amount_raw") or lt.get("tax_amount") or ""
            li_taxes.append({
                "tax_type": str(lt.get("tax_type") or "VAT").strip(),
                "tax_name": str(lt.get("tax_name") or "").strip(),
                "tax_rate": str(lt_rate).strip(),
                "tax_amount": str(lt_amt).strip(),
                "tax_type_code": str(lt.get("tax_type_code") or "").strip(),
            })

        formatted_lines.append({
            "description": str(li.get("description") or "").strip(),
            "item_type": str(li.get("item_type") or "SERVICE").strip(),
            "uom": str(li.get("uom") or "").strip(),
            "quantity": str(li.get("quantity_norm") or li.get("quantity_raw") or li.get("quantity") or "1").strip(),
            "unit_price": str(li.get("unit_price_norm") or li.get("unit_price_raw") or li.get("unit_price") or "").strip(),
            "total": str(li.get("total_norm") or li.get("total_raw") or li.get("total") or "").strip(),
            "discount": str(li.get("discount_norm") or li.get("discount_raw") or li.get("discount") or "").strip(),
            "discount_percentage": str(li.get("discount_percentage_norm") or li.get("discount_percentage_raw") or li.get("discount_percentage") or "").strip(),
            "tax_rate": str(li.get("tax_rate_norm") or li.get("tax_rate_raw") or li.get("tax_rate") or "").strip(),
            "tax_amount": str(li.get("tax_amount_norm") or li.get("tax_amount_raw") or li.get("tax_amount") or "").strip(),
            "taxes": li_taxes,
        })

    payable = {
        "invoice_number": str(d.get("invoice_number") or "").strip(),
        "invoice_date": str(d.get("invoice_date") or "").strip(),
        "due_date": str(d.get("due_date") or "").strip(),
        "invoice_type": str(d.get("invoice_type") or "INVOICE").strip(),
        "currency": str(d.get("currency") or "").strip(),
        "supplier": {
            "name": str(supp.get("name") or "").strip(),
            "supplier_id": str(supp.get("supplier_id") or "").strip(),
            "address": str(supp.get("address") or "").strip(),
            "vat_id": str(supp.get("vat_id") or "").strip(),
        },
        "buyer": {
            "company_code": str(buyer.get("company_code") or "").strip(),
            "business_unit_code": str(buyer.get("business_unit_code") or "").strip(),
            "location_code": str(buyer.get("location_code") or "").strip(),
        },
        "payment_term_id": str(d.get("payment_term_id") or "").strip(),
        "po_number": str(d.get("po_number") or "").strip(),
        "po_id": str(d.get("po_id") or "").strip(),
        "gross_total": str(d.get("gross_total_norm") or d.get("gross_total_raw") or d.get("gross_total") or "").strip(),
        "subtotal": str(d.get("subtotal_norm") or d.get("subtotal_raw") or d.get("subtotal") or "").strip(),
        "total_tax_amount": str(d.get("total_tax_norm") or d.get("total_tax_raw") or d.get("total_tax_amount") or "").strip(),
        "discount_amount": str(d.get("discount_amount_norm") or d.get("discount_amount_raw") or d.get("discount_amount") or "").strip(),
        "freight_charges": str(d.get("freight_charges_norm") or d.get("freight_charges_raw") or d.get("freight_charges") or "").strip(),
        "insurance_charges": str(d.get("insurance_charges_norm") or d.get("insurance_charges_raw") or d.get("insurance_charges") or "").strip(),
        "extra_charges": str(d.get("extra_charges_norm") or d.get("extra_charges_raw") or d.get("extra_charges") or "").strip(),
        "excise_duties": str(d.get("excise_duties_norm") or d.get("excise_duties_raw") or d.get("excise_duties") or "").strip(),
        "taxes": formatted_taxes,
        "line_items": formatted_lines,
    }

    return payable


def format_autodraft_output(
    file_name: str,
    payables: List[Dict[str, Any]],
    declined: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Construct the final output dictionary matching AUTODRAFT_SCHEMA.md:
      {
        "file": file_name,
        "payables": [...],
        "declined": [...]
      }
    """
    clean_payables = [format_autodraft_payable(p) for p in payables]
    clean_declined = [
        {
            "doc_type": str(dec.get("doc_type") or "UNKNOWN").strip(),
            "reason": str(dec.get("reason") or "").strip(),
        }
        for dec in declined
    ]

    return {
        "file": file_name,
        "payables": clean_payables,
        "declined": clean_declined,
    }


def process_document(
    pdf_path: str | Path,
    master_index: Optional[MasterDataIndex] = None,
    out_dir: Optional[str | Path] = None,
    model_name: Optional[str] = None
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Execute full end-to-end pipeline for one PDF:
      1. Classify
      2. Extract & Normalise
      3. Resolve Master Data
      4. Verify with ERP oracle (erp_book)
      5. Format final clean output
      6. Optionally save output/<pdf_stem>.json

    Returns:
      (final_output_dict, verification_results_list)
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    if master_index is None:
        master_data_dir = path.resolve().parent.parent / "master_data"
        if not master_data_dir.exists():
            master_data_dir = Path("master_data")
        master_index = MasterDataIndex(master_data_dir)

    # 1. Classify
    classification = classify_pdf(path, model_name=model_name)

    # 2. Extract
    extracted = extract_pdf(path, classification, model_name=model_name)

    # 3. Resolve Master Data on initial extraction
    for p in extracted.get("payables", []):
        resolve_master_data(p, master_index)

    # 4. Verify with ERP & execute ONE targeted diagnostic retry on mismatch
    final_payables: List[Dict[str, Any]] = []
    verification_results: List[Dict[str, Any]] = []

    for p in extracted.get("payables", []):
        formatted_initial = format_autodraft_payable(p)
        v_res = verify_payable_erp(formatted_initial)

        if v_res.get("is_match"):
            # Initial match confirmed - no retry needed
            final_payables.append(p)
            verification_results.append(v_res)
        else:
            # ERP mismatch detected -> Execute ONE generic diagnostic reread
            pages = p.get("_source_pages", [1])
            resolved_p, match_achieved, status_note = execute_generic_retry(
                pdf_path=path,
                pages=pages,
                initial_payable=p,
                master_index=master_index,
                model_name=model_name,
                format_autodraft_payable_fn=format_autodraft_payable
            )
            final_formatted = format_autodraft_payable(resolved_p)
            final_v_res = verify_payable_erp(final_formatted)
            final_payables.append(resolved_p)
            verification_results.append(final_v_res)

    # 5. Format clean output
    final_output = format_autodraft_output(
        file_name=path.name,
        payables=final_payables,
        declined=extracted.get("declined", []),
    )

    # 6. Save output if destination provided
    if out_dir is not None:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        target_file = out_path / f"{path.stem}.json"
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(final_output, f, indent=2, ensure_ascii=False)

    return final_output, verification_results
