"""
tests/test_step7_retry_policy.py

Tests proving the production-safe retry and grounding rules:
E. A grounded, semantically supported retry can replace an incorrect initial field.
F. An ungrounded retry cannot replace the initial field.
G. ERP match alone cannot justify a replacement.
H. Retry happens at most once.
I. Persistent mismatch becomes a faithful flagged result.
J. Existing passing behavior remains valid (no retry on initial match).
"""

import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock

from bookable.ground import verify_text_grounding
from bookable.retry import execute_generic_retry
from bookable.verify import verify_payable_erp
from bookable.pipeline import format_autodraft_payable


def test_rule_e_grounded_retry_replaces_field():
    """
    Test E: A grounded, semantically supported retry can replace an incorrect initial field.
    Scenario: Document evidence contains 'Trade Discount 15.00'.
    Initial extraction missed the discount. Retry discovers it, grounding passes,
    and ERP matches.
    """
    evidence = "Invoice #101 Goods: 100.00 Trade Discount: 15.00 Total Due: 85.00"
    initial_p = {
        "_segment_id": 1,
        "_segment_type": "INVOICE",
        "_source_pages": [1],
        "invoice_number": "101",
        "gross_total_raw": "85.00",
        "gross_total_norm": "85.00",
        "subtotal_raw": "100.00",
        "discount_amount_raw": None,
        "header_taxes": [],
        "line_items": [
            {
                "description": "Item 1",
                "quantity_raw": "1",
                "quantity_norm": "1",
                "unit_price_raw": "100.00",
                "unit_price_norm": "100.00",
                "total_raw": "100.00",
                "total_norm": "100.00",
                "taxes": []
            }
        ]
    }

    # Verify initial fails ERP
    initial_v = verify_payable_erp(format_autodraft_payable(initial_p))
    assert initial_v["is_match"] is False

    # Mock retry returning grounded discount 15.00
    mock_retry_raw = {
        "invoice_number": "101",
        "invoice_type": "INVOICE",
        "gross_total_raw": "85.00",
        "subtotal_raw": "100.00",
        "discount_amount_raw": "15.00",
        "header_taxes": [],
        "line_items": initial_p["line_items"],
        "_extraction_notes": "Trade Discount row identified in lower summary table: 15.00"
    }

    with patch("bookable.retry.call_vision_llm", return_value=(mock_retry_raw, {"cache_hit": False})):
        with patch("bookable.retry._render_segment_pages", return_value=([{"page_num": 1, "text": evidence, "has_text": True}], [MagicMock()])):
            resolved_p, is_match, status = execute_generic_retry(
                pdf_path="documents/INV-01.pdf",
                pages=[1],
                initial_payable=initial_p,
                format_autodraft_payable_fn=format_autodraft_payable
            )

    assert is_match is True
    assert status == "MATCH_AFTER_RETRY"
    assert resolved_p.get("discount_amount_raw") == "15.00"
    assert resolved_p.get("discount_amount_norm") == "15.00"


def test_rule_f_and_g_ungrounded_retry_rejected_despite_erp_match():
    """
    Test F & G: An ungrounded retry cannot replace an initial field, even if ERP would match!
    Scenario: Initial extraction has gross 85.00 and lines 100.00.
    Retry hallucinates 'extra_discount = 15.00' but '15.00' is NOT on the document!
    Even though 100.00 - 15.00 = 85.00 would match ERP, Rule 1 and Rule 3 require REJECTION.
    """
    evidence = "Invoice #101 Line Items Total: 100.00 Payable Total: 85.00 (No discount line visible)"
    initial_p = {
        "_segment_id": 1,
        "_segment_type": "INVOICE",
        "_source_pages": [1],
        "invoice_number": "101",
        "gross_total_raw": "85.00",
        "subtotal_raw": "100.00",
        "discount_amount_raw": None,
        "header_taxes": [],
        "line_items": [
            {
                "description": "Item 1",
                "quantity_raw": "1",
                "quantity_norm": "1",
                "unit_price_raw": "100.00",
                "unit_price_norm": "100.00",
                "total_raw": "100.00",
                "total_norm": "100.00",
                "taxes": []
            }
        ]
    }

    # Ungrounded discount amount: 15.00 is nowhere in the text
    assert verify_text_grounding("15.00", evidence) is False

    mock_retry_hallucinated = {
        "invoice_number": "101",
        "invoice_type": "INVOICE",
        "gross_total_raw": "85.00",
        "subtotal_raw": "100.00",
        "discount_amount_raw": "15.00",  # Ungrounded!
        "header_taxes": [],
        "line_items": initial_p["line_items"],
        "_extraction_notes": "Guessed 15.00 discount to make total balance"
    }

    with patch("bookable.retry.call_vision_llm", return_value=(mock_retry_hallucinated, {"cache_hit": False})):
        with patch("bookable.retry._render_segment_pages", return_value=([{"page_num": 1, "text": evidence, "has_text": True}], [MagicMock()])):
            resolved_p, is_match, status = execute_generic_retry(
                pdf_path="documents/INV-01.pdf",
                pages=[1],
                initial_payable=initial_p,
                format_autodraft_payable_fn=format_autodraft_payable
            )

    # Must be rejected because 15.00 is ungrounded
    assert is_match is False
    assert "REJECTED_UNGROUNDED" in status
    # Faithful initial extraction preserved: discount remains None
    assert resolved_p.get("discount_amount_raw") is None


def test_rule_h_and_i_retry_happens_at_most_once_persistent_mismatch_flagged():
    """
    Test H & I: Retry happens at most once. Persistent mismatch preserves faithful initial values
    and records failure without endless loops or total balancing.
    """
    evidence = "Invoice #102 Complex Tax Base 50.00 Stated Gross 99.00"
    initial_p = {
        "_segment_id": 1,
        "_segment_type": "INVOICE",
        "_source_pages": [1],
        "invoice_number": "102",
        "gross_total_raw": "99.00",
        "gross_total_norm": "99.00",
        "subtotal_raw": "50.00",
        "header_taxes": [],
        "line_items": [
            {
                "description": "Item 1",
                "quantity_raw": "1",
                "quantity_norm": "1",
                "unit_price_raw": "50.00",
                "unit_price_norm": "50.00",
                "total_raw": "50.00",
                "total_norm": "50.00",
                "taxes": []
            }
        ]
    }

    retry_call_count = 0

    def mock_llm_call(*args, **kwargs):
        nonlocal retry_call_count
        retry_call_count += 1
        return initial_p, {"cache_hit": False}

    with patch("bookable.retry.call_vision_llm", side_effect=mock_llm_call):
        with patch("bookable.retry._render_segment_pages", return_value=([{"page_num": 1, "text": evidence, "has_text": True}], [MagicMock()])):
            resolved_p, is_match, status = execute_generic_retry(
                pdf_path="documents/INV-01.pdf",
                pages=[1],
                initial_payable=initial_p,
                format_autodraft_payable_fn=format_autodraft_payable
            )

    # Exactly 1 retry executed
    assert retry_call_count == 1
    assert is_match is False
    assert "PERSISTENT_MISMATCH" in status
    # Preserves faithful original values
    assert resolved_p["gross_total_raw"] == "99.00"
    assert resolved_p["subtotal_raw"] == "50.00"


def test_rule_j_initial_match_does_not_trigger_retry():
    """
    Test J: Existing passing behavior remains valid. Documents matching ERP on initial
    extraction must never trigger a retry call.
    """
    clean_p = {
        "gross_total": "20.00",
        "subtotal": "20.00",
        "line_items": [
            {
                "description": "Item 1",
                "quantity": "2",
                "unit_price": "10.00",
                "total": "20.00",
                "taxes": []
            }
        ],
        "taxes": []
    }
    v_res = verify_payable_erp(clean_p)
    assert v_res["is_match"] is True
    assert v_res["delta"] == 0.0
