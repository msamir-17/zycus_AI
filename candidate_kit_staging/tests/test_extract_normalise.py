"""
tests/test_extract_normalise.py

Unit tests for extract.normalise_number() and extract._apply_normalisation().

These tests require NO API calls, NO PDFs, and NO external dependencies.
Run with: pytest tests/test_extract_normalise.py -v
"""
import sys
from pathlib import Path

# Ensure src/ is on the path when running from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest
from bookable.extract import normalise_number, _apply_normalisation


# ──────────────────────────────────────────────────────────────────────────────
# normalise_number tests
# ──────────────────────────────────────────────────────────────────────────────

class TestNormaliseNumber:

    # ── European locale: period-thousands + comma-decimal ──

    def test_european_thousands_and_comma_decimal(self):
        norm, raw = normalise_number("1.234,56")
        assert norm == "1234.56"
        assert raw == "1.234,56"

    def test_european_large_number(self):
        norm, raw = normalise_number("37.534,94")
        assert norm == "37534.94"

    def test_plain_comma_decimal(self):
        norm, raw = normalise_number("123,36")
        assert norm == "123.36"
        assert raw == "123,36"

    def test_negative_comma_decimal(self):
        norm, raw = normalise_number("-400,00")
        assert norm == "-400.00"

    # ── American locale: comma-thousands + dot-decimal ──

    def test_american_thousands_comma(self):
        norm, raw = normalise_number("17,657.53")
        assert norm == "17657.53"

    def test_plain_dot_decimal(self):
        norm, raw = normalise_number("292.00")
        assert norm == "292.00"

    def test_integer_string(self):
        norm, raw = normalise_number("438")
        assert norm == "438"

    # ── Currency symbols stripped ──

    def test_euro_symbol_stripped(self):
        norm, raw = normalise_number("€ 91,580.50")
        assert norm == "91580.50"

    def test_dollar_symbol_stripped(self):
        norm, raw = normalise_number("$17,657.53")
        assert norm == "17657.53"

    # ── Percentage stripped ──

    def test_percentage_stripped(self):
        norm, raw = normalise_number("19%")
        assert norm == "19"

    def test_percentage_decimal(self):
        norm, raw = normalise_number("7.5%")
        assert norm == "7.5"

    # ── Edge cases ──

    def test_empty_string(self):
        norm, raw = normalise_number("")
        assert norm == ""

    def test_none_input(self):
        norm, raw = normalise_number(None)
        assert norm == ""
        assert raw == ""

    def test_zero(self):
        norm, raw = normalise_number("0.00")
        assert norm == "0.00"

    def test_negative_dot_decimal(self):
        norm, raw = normalise_number("-400.00")
        assert norm == "-400.00"

    def test_already_clean(self):
        norm, raw = normalise_number("73.00")
        assert norm == "73.00"

    def test_whitespace_stripped(self):
        norm, raw = normalise_number("  146.00  ")
        assert norm == "146.00"


# ──────────────────────────────────────────────────────────────────────────────
# _apply_normalisation tests
# ──────────────────────────────────────────────────────────────────────────────

class TestApplyNormalisation:

    def test_top_level_raw_fields(self):
        d = {
            "gross_total_raw": "17,657.53",
            "subtotal_raw": "1.234,56",
            "freight_charges_raw": "",
        }
        _apply_normalisation(d)
        assert d["gross_total_norm"] == "17657.53"
        assert d["subtotal_norm"] == "1234.56"
        assert d["freight_charges_norm"] == ""   # empty stays empty

    def test_line_items_normalised(self):
        d = {
            "line_items": [
                {
                    "quantity_raw": "2",
                    "unit_price_raw": "73,00",
                    "total_raw": "146,00",
                    "taxes": [
                        {"tax_rate_raw": "19%", "tax_amount_raw": "27,74"}
                    ]
                }
            ]
        }
        _apply_normalisation(d)
        li = d["line_items"][0]
        assert li["quantity_norm"] == "2"
        assert li["unit_price_norm"] == "73.00"
        assert li["total_norm"] == "146.00"
        tax = li["taxes"][0]
        assert tax["tax_rate_norm"] == "19"
        assert tax["tax_amount_norm"] == "27.74"

    def test_header_taxes_normalised(self):
        d = {
            "header_taxes": [
                {"tax_rate_raw": "22%", "tax_amount_raw": "-88,00"}
            ]
        }
        _apply_normalisation(d)
        t = d["header_taxes"][0]
        assert t["tax_rate_norm"] == "22"
        assert t["tax_amount_norm"] == "-88.00"

    def test_no_raw_keys_untouched(self):
        d = {"invoice_number": "39015", "invoice_type": "INVOICE"}
        _apply_normalisation(d)
        # No _norm keys should be added for non-_raw keys
        assert "invoice_number_norm" not in d
        assert "invoice_type_norm" not in d

    def test_credit_memo_negative_amount(self):
        d = {"gross_total_raw": "-400,00"}
        _apply_normalisation(d)
        assert d["gross_total_norm"] == "-400.00"


# ──────────────────────────────────────────────────────────────────────────────
# Additional boundary tests for locale disambiguation
# ──────────────────────────────────────────────────────────────────────────────

class TestLocaleDisambiguation:
    """
    These test the locale detection logic directly.
    The key rule: "1.234,56" → European (thousands=period, decimal=comma).
                  "1,234.56" → American (thousands=comma, decimal=period).
    """

    def test_european_locale_4_digit_integer_part(self):
        norm, _ = normalise_number("1.234,56")
        assert norm == "1234.56"

    def test_american_locale_4_digit_integer_part(self):
        norm, _ = normalise_number("1,234.56")
        assert norm == "1234.56"

    def test_small_comma_decimal_two_digits(self):
        # "13,98" → 13.98 (not 1398)
        norm, _ = normalise_number("13,98")
        assert norm == "13.98"

    def test_thai_amount_no_locale_issue(self):
        # Thai amounts often use plain dot-decimal
        norm, _ = normalise_number("78480.00")
        assert norm == "78480.00"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
