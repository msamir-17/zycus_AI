"""
tests/test_step5_fixes.py

Focused unit tests for Step 5 general fixes:
1. Provider fallback (Gemini -> OpenAI)
2. 429 / Quota handling and circuit breaker
3. Cache reuse via content SHA-256
4. API usage logging & metadata
5. Raw amount normalisation (Decimal, comma-decimal, dot-thousands)
6. Ambiguous numeric formats (flagged, never guessed)
7. Credit memo positive magnitude sign handling
8. Unit price derivation flag (unit_price_derived)
9. General segmentation rule (Section 9 - parent/attachment consolidation)
10. Tax and charge level fidelity
"""

import pytest
from decimal import Decimal
from PIL import Image

from bookable.ground import normalise_financial_amount, derive_unit_price
from bookable.extract import _apply_normalisation
from bookable.llm import compute_cache_key, call_vision_llm, _image_to_base64
from erp import erp_book


# ──────────────────────────────────────────────────────────────────────────────
# 1 & 2. Provider Fallback, 429 Quota Handling & Circuit Breaker
# ──────────────────────────────────────────────────────────────────────────────

def test_cache_identity_and_reuse():
    """Verify SHA-256 cache identity is stable for identical inputs."""
    img1 = Image.new("RGB", (50, 50), color="blue")
    img2 = Image.new("RGB", (50, 50), color="blue")
    k1 = compute_cache_key("Extract invoice", [img1], text_hints=["Page 1"])
    k2 = compute_cache_key("Extract invoice", [img2], text_hints=["Page 1"])
    assert k1 == k2, "Identical prompts and images must yield identical cache keys"

    img_diff = Image.new("RGB", (50, 50), color="red")
    k3 = compute_cache_key("Extract invoice", [img_diff], text_hints=["Page 1"])
    assert k1 != k3, "Different image content must yield different cache keys"


def test_base64_image_conversion():
    """Verify PIL images convert cleanly to valid Base64 JPEG strings."""
    img = Image.new("RGB", (10, 10), color="green")
    b64 = _image_to_base64(img)
    assert isinstance(b64, str)
    assert len(b64) > 20


# ──────────────────────────────────────────────────────────────────────────────
# 5. Raw Amount Normalisation (Decimal, comma-decimal, dot-thousands)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw_input, expected_norm, expected_dec", [
    ("771,66", "771.66", Decimal("771.66")),
    ("1.234,56", "1234.56", Decimal("1234.56")),
    ("327,87", "327.87", Decimal("327.87")),
    ("77166", "77166", Decimal("77166")),
    ("1,234.56", "1234.56", Decimal("1234.56")),
    ("17,657.53", "17657.53", Decimal("17657.53")),
    ("€ 1.234,56", "1234.56", Decimal("1234.56")),
    ("$ 17,657.53", "17657.53", Decimal("17657.53")),
    ("22%", "22", Decimal("22")),
    ("-400,00", "-400.00", Decimal("-400.00")),
])
def test_normalise_financial_amount_valid(raw_input, expected_norm, expected_dec):
    dec, norm_str, is_amb = normalise_financial_amount(raw_input)
    assert not is_amb, f"{raw_input} should not be flagged as ambiguous"
    assert norm_str == expected_norm
    assert dec == expected_dec


# ──────────────────────────────────────────────────────────────────────────────
# 6. Ambiguous Numeric Formats (Flagged, never guessed)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ambiguous_input", [
    "123,456",
    "123.456",
    "999.000",
    "100,000",
])
def test_ambiguous_financial_amount(ambiguous_input):
    """Numbers with single separator followed by exactly 3 digits are ambiguous."""
    dec, norm_str, is_amb = normalise_financial_amount(ambiguous_input)
    assert is_amb is True, f"{ambiguous_input} must be flagged as ambiguous"
    assert dec is None, "Ambiguous numbers must not guess Decimal value"
    assert norm_str == ambiguous_input, "Ambiguous values must preserve raw text"


# ──────────────────────────────────────────────────────────────────────────────
# 7. Credit Memo Positive Magnitude Normalisation
# ──────────────────────────────────────────────────────────────────────────────

def test_credit_memo_positive_magnitudes():
    """Verify that credit memo raw negative amounts normalize to positive magnitudes for ERP."""
    extracted = {
        "invoice_number": "CM-991",
        "invoice_type": "CREDIT_MEMO",
        "gross_total_raw": "-400,00",
        "subtotal_raw": "-327,87",
        "header_taxes": [
            {"tax_name": "KM", "tax_rate_raw": "22%", "tax_amount_raw": "-72,13"}
        ],
        "line_items": [
            {
                "description": "Returned Item",
                "quantity_raw": "1",
                "unit_price_raw": "-327,87",
                "total_raw": "-327,87",
                "taxes": []
            }
        ]
    }
    _apply_normalisation(extracted)

    assert extracted["gross_total_norm"] == "400.00"
    assert extracted["subtotal_norm"] == "327.87"
    assert extracted["header_taxes"][0]["tax_amount_norm"] == "72.13"
    li = extracted["line_items"][0]
    assert li["unit_price_norm"] == "327.87"
    assert li["total_norm"] == "327.87"

    # Verify erp_book agrees with positive magnitudes
    test_p = {
        "currency": "EUR",
        "line_items": [{"quantity": li["quantity_norm"], "unit_price": li["unit_price_norm"]}],
        "taxes": [{"tax_name": "KM", "tax_rate": "22", "tax_amount": extracted["header_taxes"][0]["tax_amount_norm"]}]
    }
    res = erp_book(test_p)
    assert res["will_book_gross"] == 400.00


# ──────────────────────────────────────────────────────────────────────────────
# 8. Missing Unit Price & Derived Flag
# ──────────────────────────────────────────────────────────────────────────────

def test_unit_price_derivation_and_flag():
    """Verify missing unit_price is derived when total and qty exist, and flagged as derived."""
    # Case A: Missing unit price, single line qty=1, total=91,580.50
    price, is_derived = derive_unit_price(quantity_raw="1", unit_price_raw=None, line_total_raw="91,580.50")
    assert is_derived is True
    assert price == "91580.5"

    # Case B: Explicit unit price printed
    price2, is_derived2 = derive_unit_price(quantity_raw="12", unit_price_raw="600.00", line_total_raw="7,200.00")
    assert is_derived2 is False
    assert price2 == "600.00"

    # Case C: Apply normalisation on payable dict
    doc_data = {
        "invoice_type": "INVOICE",
        "gross_total_raw": "91,580.50",
        "line_items": [
            {
                "quantity_raw": "1",
                "unit_price_raw": None,
                "total_raw": "91,580.50",
                "taxes": []
            }
        ]
    }
    _apply_normalisation(doc_data)
    li = doc_data["line_items"][0]
    assert li["unit_price_norm"] == "91580.5"
    assert li["unit_price_derived"] is True

    # ERP computes full 91,580.50 instead of 0.0
    res = erp_book({
        "line_items": [{"quantity": "1", "unit_price": li["unit_price_norm"]}],
        "taxes": []
    })
    assert res["will_book_gross"] == 91580.50


# ──────────────────────────────────────────────────────────────────────────────
# 9. General Segmentation Rule (Rule 9 / Project Brief)
# ──────────────────────────────────────────────────────────────────────────────

def test_inv31_general_segmentation_rule():
    """
    Verify that general segmentation logic treats pass-through utility backups
    (same PO/amount, backing up parent vendor) as attachments, not separate payables.
    """
    # A multi-page document structure representing INV-31
    doc_segments = [
        {
            "segment_id": 1,
            "type": "INVOICE",
            "pages": [1],
            "description": "Invoice 39015 from 100 Airport KPG III LLC",
            "invoice_number": "39015",
            "seller": "100 Airport KPG III LLC",
            "amount": "17657.53"
        },
        {
            "segment_id": 2,
            "type": "SUPPORTING_DOC",
            "pages": [2, 3],
            "description": "Backup utility bill from PECO Energy attached for reference",
            "invoice_number": "PO#16299",
            "seller": "PECO",
            "amount": "17657.53"
        }
    ]

    # Deterministic consolidation: if segment 2 is supporting/backup of segment 1 with identical amount
    def consolidate_segments(segments):
        if len(segments) <= 1:
            return segments
        parent = segments[0]
        consolidated_pages = list(parent["pages"])
        for s in segments[1:]:
            if s.get("type") in ("SUPPORTING_DOC", "ATTACHMENT") or s.get("amount") == parent.get("amount"):
                consolidated_pages.extend(s.get("pages", []))
        parent["pages"] = sorted(list(set(consolidated_pages)))
        return [parent]

    consolidated = consolidate_segments(doc_segments)
    assert len(consolidated) == 1, "INV-31 style parent+attachment must consolidate into 1 payable"
    assert consolidated[0]["pages"] == [1, 2, 3]


# ──────────────────────────────────────────────────────────────────────────────
# 10. Tax and Charge Level Fidelity
# ──────────────────────────────────────────────────────────────────────────────

def test_tax_and_charge_fidelity():
    """Verify that line taxes, header taxes, extra charges, and withholding remain at their assigned levels."""
    hld01_payable = {
        "invoice_type": "INVOICE",
        "gross_total_raw": "8,161.92",
        "subtotal_raw": "7,200.00",
        "extra_charges_raw": "648.00",
        "header_taxes": [
            {"tax_name": "VAT", "tax_rate_raw": "7%", "tax_amount_raw": "549.36"},
            {"tax_name": "WITHHOLDING TAX", "tax_rate_raw": "3%", "tax_amount_raw": "-235.44"}
        ],
        "line_items": [
            {
                "description": "Staff 2 Units X 6 Days",
                "quantity_raw": "12",
                "unit_price_raw": "600.00",
                "total_raw": "7,200.00",
                "taxes": []
            }
        ]
    }
    _apply_normalisation(hld01_payable)

    assert hld01_payable["extra_charges_norm"] == "648.00"
    assert hld01_payable["header_taxes"][0]["tax_amount_norm"] == "549.36"
    assert hld01_payable["header_taxes"][1]["tax_amount_norm"] == "-235.44"

    test_erp = {
        "extra_charges": hld01_payable["extra_charges_norm"],
        "taxes": [
            {"tax_name": "VAT", "tax_rate": "7", "tax_amount": "549.36"},
            {"tax_name": "WHT", "tax_rate": "3", "tax_amount": "-235.44"}
        ],
        "line_items": [{"quantity": "12", "unit_price": "600.00"}]
    }
    res = erp_book(test_erp)
    assert res["will_book_gross"] == 8161.92


# ──────────────────────────────────────────────────────────────────────────────
# 11. OpenAI Detail Modes (Low for Classification, High for Extraction)
# ──────────────────────────────────────────────────────────────────────────────

from unittest.mock import patch, MagicMock
from bookable.llm import _parse_retry_delay, set_gemini_circuit_broken, get_gemini_circuit_broken, call_openai_vision
import httpx


def test_retry_after_parsing():
    """Verify _parse_retry_delay extracts wait time from headers and body."""
    # Case 1: Retry-After header
    resp1 = MagicMock()
    resp1.headers = {"retry-after": "4.5"}
    assert _parse_retry_delay(resp1) == 4.5

    # Case 2: retry-after-ms header
    resp2 = MagicMock()
    resp2.headers = {"retry-after-ms": "2500"}
    assert _parse_retry_delay(resp2) == 2.5

    # Case 3: Body text "try again in 2.929s"
    resp3 = MagicMock()
    resp3.headers = {}
    resp3.text = '{"error": {"message": "Rate limit reached. Please try again in 2.929s."}}'
    assert _parse_retry_delay(resp3) == 2.929

    # Case 4: Body text "try again in 404ms"
    resp4 = MagicMock()
    resp4.headers = {}
    resp4.text = '{"error": {"message": "Rate limit reached. Please try again in 404ms."}}'
    assert _parse_retry_delay(resp4) == 0.404


def test_openai_429_temporary_retry():
    """Verify that call_openai_vision retries on 429 and succeeds on subsequent 200."""
    img = Image.new("RGB", (20, 20), color="white")

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"retry-after-ms": "50"}
    resp_429.text = '{"error": {"message": "Please try again in 50ms."}}'

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [{"message": {"content": '{"status": "ok_after_retry"}'}}],
        "usage": {"total_tokens": 100}
    }

    with patch("httpx.post", side_effect=[resp_429, resp_200]) as mock_post:
        data, meta = call_openai_vision("Test prompt", [img], detail="low", max_retries=2)
        assert data["status"] == "ok_after_retry"
        assert meta["retries"] == 1
        assert meta["detail"] == "low"
        assert mock_post.call_count == 2


def test_openai_429_retry_limit_exceeded():
    """Verify that call_openai_vision raises RuntimeError after exhausting max_retries."""
    img = Image.new("RGB", (20, 20), color="white")

    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {"retry-after-ms": "10"}
    resp_429.text = 'Rate limit reached'

    with patch("httpx.post", return_value=resp_429):
        with pytest.raises(RuntimeError, match="OpenAI API error 429"):
            call_openai_vision("Test prompt", [img], detail="low", max_retries=2)


def test_gemini_daily_quota_circuit_breaker():
    """Verify that RESOURCE_EXHAUSTED activates the circuit-breaker and subsequent calls skip Gemini."""
    set_gemini_circuit_broken(False)
    assert get_gemini_circuit_broken() is False

    img = Image.new("RGB", (20, 20), color="white")

    # Mock call_gemini_vision raising daily quota
    with patch("bookable.llm.call_gemini_vision", side_effect=Exception("429 RESOURCE_EXHAUSTED. Quota exceeded")):
        with patch("bookable.llm.call_openai_vision", return_value=({"status": "openai_success"}, {"provider": "openai"})):
            data, meta = call_vision_llm("Prompt", [img], use_cache=False)
            assert data["status"] == "openai_success"
            assert get_gemini_circuit_broken() is True

    # Next call should route directly to OpenAI without calling call_gemini_vision
    with patch("bookable.llm.call_gemini_vision") as mock_gemini:
        with patch("bookable.llm.call_openai_vision", return_value=({"status": "openai_direct"}, {"provider": "openai"})):
            data2, meta2 = call_vision_llm("Prompt 2", [img], use_cache=False)
            assert data2["status"] == "openai_direct"
            assert mock_gemini.call_count == 0, "Gemini must not be called when circuit breaker is active"

    # Reset circuit breaker
    set_gemini_circuit_broken(False)


def test_classification_detail_low_and_extraction_detail_high():
    """Verify that classify_pdf passes detail='low' and extract_segment passes detail='high'."""
    from bookable.classify import classify_pdf
    from bookable.extract import extract_segment
    from pathlib import Path

    doc_path = Path("documents/DU-11.pdf")
    if not doc_path.exists():
        pytest.skip("DU-11.pdf not found in documents/")

    # 1. Verify classify_pdf passes detail="low"
    with patch("bookable.classify.call_vision_llm") as mock_llm_classify:
        mock_llm_classify.return_value = ({"filename": "DU-11.pdf", "decision": "PROCESS", "document_type": "INVOICE"}, {"provider": "test"})
        classify_pdf(doc_path)
        assert mock_llm_classify.called
        _, kwargs = mock_llm_classify.call_args
        assert kwargs.get("detail") == "low", f"classify_pdf must specify detail='low', got {kwargs.get('detail')}"

    # 2. Verify extract_segment passes detail="high"
    seg = {"segment_id": 1, "type": "INVOICE", "pages": [1], "description": "Test segment"}
    with patch("bookable.extract.call_vision_llm") as mock_llm_extract:
        mock_llm_extract.return_value = ({"invoice_number": "TEST-1", "invoice_type": "INVOICE"}, {"provider": "test"})
        extract_segment(doc_path, seg)
        assert mock_llm_extract.called
        _, kwargs2 = mock_llm_extract.call_args
        assert kwargs2.get("detail") == "high", f"extract_segment must specify detail='high', got {kwargs2.get('detail')}"


# ──────────────────────────────────────────────────────────────────────────────
# 12. Quality Fixes Unit Tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw_input,expected_str,expected_dec", [
    ("R148,941.47", "148941.47", Decimal("148941.47")),
    ("KES 70,654.30", "70654.30", Decimal("70654.30")),
    ("GHC 1,234.56", "1234.56", Decimal("1234.56")),
    ("EUR 1.234,56", "1234.56", Decimal("1234.56")),
    ("152 587.46", "152587.46", Decimal("152587.46")),
    ("152 587,46", "152587.46", Decimal("152587.46")),
])
def test_extended_currency_and_number_normalization(raw_input, expected_str, expected_dec):
    """Verify normalization of extended currencies and space-delimited thousands."""
    dec_val, norm_str, is_amb = normalise_financial_amount(raw_input)
    assert not is_amb
    assert norm_str == expected_str
    assert dec_val == expected_dec


def test_fractional_quantity_normalization():
    """Verify fractional quantities e.g. '1/1', '2/2' only in quantity field."""
    from bookable.extract import normalise_quantity_field
    assert normalise_quantity_field("1/1")[1] == "1"
    assert normalise_quantity_field("2/2")[1] == "2"
    assert normalise_quantity_field("  4/4 ")[1] == "4"
    assert normalise_quantity_field("12")[1] == "12"
    assert normalise_quantity_field(None)[1] == "1"


def test_tax_duplication_deduplication():
    """Verify that when document evidence shows header tax is a duplicate summary of line taxes, header duplicate is removed."""
    from bookable.extract import _apply_normalisation

    doc_data = {
        "invoice_type": "INVOICE",
        "gross_total_raw": "5,076.17",
        "subtotal_raw": "4,230.14",
        "header_taxes": [
            {"tax_name": "VAT", "tax_rate_raw": "20%", "tax_amount_raw": "846.03"}
        ],
        "line_items": [
            {
                "description": "Item 1",
                "quantity_raw": "6",
                "unit_price_raw": "457.24",
                "total_raw": "2,743.44",
                "taxes": [{"tax_name": "VAT", "tax_rate_raw": "20%", "tax_amount_raw": "548.69"}]
            },
            {
                "description": "Item 2",
                "quantity_raw": "2",
                "unit_price_raw": "743.35",
                "total_raw": "1,486.70",
                "taxes": [{"tax_name": "VAT", "tax_rate_raw": "20%", "tax_amount_raw": "297.34"}]
            }
        ]
    }
    _apply_normalisation(doc_data)

    # Line taxes must be preserved
    assert len(doc_data["line_items"][0]["taxes"]) == 1
    assert doc_data["line_items"][0]["taxes"][0]["tax_amount_norm"] == "548.69"
    assert doc_data["line_items"][1]["taxes"][0]["tax_amount_norm"] == "297.34"

    # Header summary tax must be deduplicated
    assert len(doc_data["header_taxes"]) == 0
    assert doc_data.get("_tax_deduplicated") is True

    # ERP books exact penny match
    erp_p = {
        "taxes": [],
        "line_items": [
            {
                "quantity": doc_data["line_items"][0]["quantity_norm"],
                "unit_price": doc_data["line_items"][0]["unit_price_norm"],
                "taxes": [{"tax_name": "VAT", "tax_amount": "548.69"}]
            },
            {
                "quantity": doc_data["line_items"][1]["quantity_norm"],
                "unit_price": doc_data["line_items"][1]["unit_price_norm"],
                "taxes": [{"tax_name": "VAT", "tax_amount": "297.34"}]
            }
        ]
    }
    res = erp_book(erp_p)
    assert res["will_book_gross"] == 5076.17


def test_tax_placement_ambiguous_preservation():
    """Verify that when tax sums do not match and are not duplicate summaries, evidence is preserved and flagged."""
    from bookable.extract import _apply_normalisation

    doc_data = {
        "invoice_type": "INVOICE",
        "gross_total_raw": "1,000.00",
        "header_taxes": [
            {"tax_name": "Service Tax", "tax_rate_raw": "10%", "tax_amount_raw": "100.00"}
        ],
        "line_items": [
            {
                "description": "Item 1",
                "quantity_raw": "1",
                "unit_price_raw": "800.00",
                "total_raw": "800.00",
                "taxes": [{"tax_name": "City Levy", "tax_rate_raw": "2%", "tax_amount_raw": "16.00"}]
            }
        ]
    }
    _apply_normalisation(doc_data)

    # Both distinct taxes must be preserved, and flagged as ambiguous
    assert len(doc_data["header_taxes"]) == 1
    assert len(doc_data["line_items"][0]["taxes"]) == 1
    assert doc_data.get("tax_placement_ambiguous") is True


def test_column_leakage_protection_discount_and_quantity():
    """Verify that tax rate leaked into discount % or quantity is cleaned."""
    from bookable.extract import _apply_normalisation

    doc_data = {
        "invoice_type": "INVOICE",
        "header_taxes": [{"tax_name": "IVA", "tax_rate_raw": "23%", "tax_amount_raw": "32.91"}],
        "line_items": [
            {
                "description": "PHOTOCOPY SERVICE",
                "quantity_raw": "23,0",  # Leaked tax rate into quantity when unit price equals total
                "unit_price_raw": "160.00",
                "total_raw": "160.00",
                "discount_percentage_raw": "23%",  # Leaked tax rate into discount %
                "taxes": [{"tax_name": "IVA", "tax_rate_raw": "23%", "tax_amount_raw": "36.80"}]
            }
        ]
    }
    _apply_normalisation(doc_data)

    li = doc_data["line_items"][0]
    # Discount percentage cleared because 23% was the tax rate and no discount keyword exists
    assert li["discount_percentage_norm"] == ""
    assert li.get("discount_percentage_cleared_tax_leakage") is True
    # Quantity corrected to 1 because 23,0 was the tax rate and total equals unit price
    assert li["quantity_norm"] == "1"
    assert li.get("quantity_corrected_from_tax_rate") is True


def test_cache_identity_across_schema_prompt_detail_content():
    """Verify that compute_cache_key produces distinct hashes for schema, prompt, detail, and image content."""
    from bookable.llm import compute_cache_key
    from PIL import Image

    img1 = Image.new("RGB", (10, 10), color="red")
    img2 = Image.new("RGB", (10, 10), color="blue")

    key_base = compute_cache_key("Prompt A", [img1], detail="high")
    key_diff_prompt = compute_cache_key("Prompt B", [img1], detail="high")
    key_diff_detail = compute_cache_key("Prompt A", [img1], detail="low")
    key_diff_img = compute_cache_key("Prompt A", [img2], detail="high")

    assert key_base != key_diff_prompt
    assert key_base != key_diff_detail
    assert key_base != key_diff_img

