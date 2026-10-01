"""
tests/test_output_schema.py

Validates the final output structure:
  1. Conforms strictly to AUTODRAFT_SCHEMA.md and sample_autodraft.json.
  2. Recursively asserts that zero internal diagnostic keys (starting with '_') are present.
  3. Verifies master-data resolution populates codes when grounded and leaves honest blanks ("") when unmatched.
"""

from pathlib import Path
from typing import Any, Dict, List
import pytest

from bookable.pipeline import format_autodraft_output, format_autodraft_payable
from bookable.resolve import MasterDataIndex, resolve_master_data


def _find_private_keys(obj: Any, path: str = "") -> List[str]:
    """Recursively search for any dictionary keys starting with '_'."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            current_path = f"{path}.{k}" if path else k
            if k.startswith("_"):
                found.append(current_path)
            found.extend(_find_private_keys(v, current_path))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            current_path = f"{path}[{idx}]"
            found.extend(_find_private_keys(item, current_path))
    return found


def test_schema_conformance_and_zero_private_keys():
    """Verify that format_autodraft_output produces the exact schema with zero private keys."""
    raw_payable = {
        "_segment_id": 1,
        "_source_pages": [1, 2],
        "_llm_usage": {"tokens": 200},
        "_decision": "PROCESS",
        "_pdf_file": "INV-TEST.pdf",
        "_extraction_notes": "Internal notes here",
        "gross_total_raw": "1.000,00",
        "gross_total_norm": "1000.00",
        "subtotal_norm": "900.00",
        "total_tax_norm": "100.00",
        "invoice_number": "INV-2026-001",
        "invoice_date": "2026-03-01",
        "due_date": "2026-03-31",
        "invoice_type": "INVOICE",
        "currency": "EUR",
        "supplier": {
            "name": "Test Supplier",
            "vat_id": "DE123456789",
            "address": "Berlin, Germany",
            "_confidence": "high"
        },
        "buyer": {
            "name": "Bolt Technology OU",
            "address": "Tallinn, Estonia"
        },
        "payment_terms_raw": "30 days",
        "po_number": "PO-999",
        "header_taxes": [
            {
                "tax_name": "VAT",
                "tax_rate_norm": "20",
                "tax_amount_norm": "100.00",
                "tax_type_code": "EST_200_VAT",
                "_placement": "HEADER"
            }
        ],
        "line_items": [
            {
                "description": "Consulting services",
                "item_type": "SERVICE",
                "uom": "Hr",
                "quantity_norm": "10",
                "unit_price_norm": "90.00",
                "total_norm": "900.00",
                "_derived_unit_price": False,
                "taxes": [
                    {
                        "tax_name": "VAT",
                        "tax_rate_norm": "20",
                        "tax_amount_norm": "100.00",
                        "_placement": "LINE"
                    }
                ]
            }
        ]
    }

    declined_raw = [
        {
            "doc_type": "STATEMENT",
            "reason": "Account summary statement without individual line items",
            "_evidence": "Found statement text on page 1",
            "_source_pages": [1]
        }
    ]

    output = format_autodraft_output("INV-TEST.pdf", [raw_payable], declined_raw)

    # 1. Check top-level contract
    assert output["file"] == "INV-TEST.pdf"
    assert isinstance(output["payables"], list) and len(output["payables"]) == 1
    assert isinstance(output["declined"], list) and len(output["declined"]) == 1

    # 2. Check no private keys exist anywhere
    private_keys = _find_private_keys(output)
    assert not private_keys, f"Found private diagnostic keys in final output: {private_keys}"

    # 3. Check payable schema
    p = output["payables"][0]
    expected_top_keys = {
        "invoice_number", "invoice_date", "due_date", "invoice_type", "currency",
        "supplier", "buyer", "payment_term_id", "po_number", "po_id",
        "gross_total", "subtotal", "total_tax_amount", "discount_amount",
        "freight_charges", "insurance_charges", "extra_charges", "excise_duties",
        "taxes", "line_items"
    }
    assert set(p.keys()) == expected_top_keys
    assert set(p["supplier"].keys()) == {"name", "supplier_id", "address", "vat_id"}
    assert set(p["buyer"].keys()) == {"company_code", "business_unit_code", "location_code"}

    # 4. Check tax shape
    assert len(p["taxes"]) == 1
    assert set(p["taxes"][0].keys()) == {"tax_type", "tax_name", "tax_rate", "tax_amount", "tax_type_code"}

    # 5. Check line item shape
    assert len(p["line_items"]) == 1
    li = p["line_items"][0]
    expected_li_keys = {
        "description", "item_type", "uom", "quantity", "unit_price", "total",
        "discount", "discount_percentage", "tax_rate", "tax_amount", "taxes"
    }
    assert set(li.keys()) == expected_li_keys
    assert set(li["taxes"][0].keys()) == {"tax_type", "tax_name", "tax_rate", "tax_amount", "tax_type_code"}

    # 6. Check declined document shape
    dec = output["declined"][0]
    assert set(dec.keys()) == {"doc_type", "reason"}
    assert dec["doc_type"] == "STATEMENT"


def test_master_data_resolution_grounding_and_honest_blanks():
    """Verify master data resolves known entries and leaves honest blanks for unknown entries."""
    repo_root = Path(__file__).resolve().parent.parent
    master_index = MasterDataIndex(repo_root / "master_data")

    # Case A: Known grounded master data
    known_doc = {
        "supplier": {
            "name": "Phocus Direct Communication GmbH",
            "vat_id": "DE209177122",
            "address": "Lina-Ammon-Strasse 19b, 90471 Nurnberg, DE"
        },
        "buyer": {
            "name": "Bolt Operations UK Ltd",
            "address": "5 New Street Square, London, UK"
        },
        "payment_terms_raw": "Net 10",
        "po_number": "PO-EE-2026-0044",
        "header_taxes": [
            {
                "tax_name": "German VAT 19%",
                "tax_rate_norm": "19",
                "tax_amount_norm": "76.00"
            }
        ],
        "line_items": []
    }

    resolve_master_data(known_doc, master_index, country_hint="DE")
    assert known_doc["supplier"]["supplier_id"] == "2845695"
    assert known_doc["buyer"]["company_code"] == "BOLTGROUP"
    assert known_doc["buyer"]["business_unit_code"] == "GB001"
    assert known_doc["buyer"]["location_code"] == "LOC_GB_001"
    assert known_doc["payment_term_id"] == "Net_10"
    assert known_doc["po_id"] == "PO-EE-2026-0044"
    assert known_doc["header_taxes"][0]["tax_type_code"] == "DE_190_VAT"

    # Case B: Unknown/Unmatched document -- must remain honest blanks ""
    unknown_doc = {
        "supplier": {
            "name": "Unknown Global Services Inc",
            "vat_id": "US999999999",
            "address": "Seattle, WA, USA"
        },
        "buyer": {
            "name": "External Non-Tenant Corp",
            "address": "Tokyo, Japan"
        },
        "payment_terms_raw": "custom terms",
        "po_number": "PO-UNKNOWN-8888",
        "header_taxes": [
            {
                "tax_name": "Special Tax",
                "tax_rate_norm": "11.5",
                "tax_amount_norm": "11.50"
            }
        ],
        "line_items": []
    }

    resolve_master_data(unknown_doc, master_index)
    assert unknown_doc["supplier"]["supplier_id"] == ""
    assert unknown_doc["buyer"]["company_code"] == ""
    assert unknown_doc["buyer"]["business_unit_code"] == ""
    assert unknown_doc["buyer"]["location_code"] == ""
    assert unknown_doc["payment_term_id"] == ""
    assert unknown_doc["po_id"] == ""
    assert unknown_doc["header_taxes"][0]["tax_type_code"] == ""
