"""
tests/test_step7_unit_price_regression.py

Unit and regression tests for unit_price derivation rules:
1. INV-20 regression: Explicit unit_price (11.19) must NOT be overwritten with line_total (12.10)
   even when qty=1, tax=0.91, line_total=12.10.
2. Derivation permitted ONLY when unit_price is absent, null, or empty.
3. PU / price-per-100 handling: explicit unit price preserved.
4. Tax-inclusive line totals: explicit unit price preserved.
5. Normal missing unit price derivation works as expected.
6. Missing quantity or total prevents derivation (returns None, False).
"""

import pytest
from decimal import Decimal
from bookable.ground import derive_unit_price, normalise_financial_amount


def test_inv20_direct_regression():
    """
    INV-20 direct regression test:
    qty = 1, unit_price = 11.19, tax = 0.91, line_total = 12.10.
    The explicitly extracted unit_price 11.19 must be preserved and NOT overwritten by 12.10.
    """
    qty = "1"
    unit_price = "11.19"
    line_total = "12.10"

    price, is_derived = derive_unit_price(
        quantity_raw=qty,
        unit_price_raw=unit_price,
        line_total_raw=line_total,
        discount_percentage_raw=None,
        discount_raw=None
    )

    assert price == "11.19", f"Expected explicit unit price '11.19', got '{price}'"
    assert is_derived is False, "Explicit unit price must NOT be flagged as derived"


def test_tax_inclusive_line_total_preserves_explicit_unit_price():
    """
    When item prices are tax-inclusive (e.g. qty=2, unit_price=10.00 net, total=24.00 inclusive of 20% VAT),
    the explicit unit_price must NOT be replaced with total/qty.
    """
    price, is_derived = derive_unit_price(
        quantity_raw="2",
        unit_price_raw="10.00",
        line_total_raw="24.00"
    )
    assert price == "10.00"
    assert is_derived is False


def test_price_per_hundred_pu_preserved():
    """
    PU / Price-per-100 items:
    qty = 500, unit_price = 20.00 (quoted per 100), line_total = 100.00.
    Explicit unit_price 20.00 must NOT be overwritten by 100.00 / 500 = 0.20.
    """
    price, is_derived = derive_unit_price(
        quantity_raw="500",
        unit_price_raw="20.00",
        line_total_raw="100.00"
    )
    assert price == "20.00"
    assert is_derived is False


def test_normal_missing_unit_price_derivation():
    """
    When unit_price_raw is absent/null/empty:
    Derivation is permitted if both quantity and line_total are present and non-zero.
    """
    price, is_derived = derive_unit_price(
        quantity_raw="4",
        unit_price_raw=None,
        line_total_raw="100.00"
    )
    assert price == "25"
    assert is_derived is True

    # Empty string should also trigger derivation
    price_empty, is_derived_empty = derive_unit_price(
        quantity_raw="5",
        unit_price_raw="",
        line_total_raw="50.00"
    )
    assert price_empty == "10"
    assert is_derived_empty is True


def test_missing_quantity_or_total_prevents_derivation():
    """
    When unit_price_raw is absent, if either quantity or line_total is absent or zero,
    derivation must NOT occur.
    """
    price1, is_derived1 = derive_unit_price(
        quantity_raw=None,
        unit_price_raw=None,
        line_total_raw="50.00"
    )
    assert price1 is None
    assert is_derived1 is False

    price2, is_derived2 = derive_unit_price(
        quantity_raw="5",
        unit_price_raw=None,
        line_total_raw=None
    )
    assert price2 is None
    assert is_derived2 is False

    price3, is_derived3 = derive_unit_price(
        quantity_raw="0",
        unit_price_raw=None,
        line_total_raw="100.00"
    )
    assert price3 is None
    assert is_derived3 is False


def test_lot_batch_pricing_per_units_still_supported():
    """
    Existing DU-05 lot pricing behavior:
    When quantity > 1 and unit_price equals line_total (total copied into unit price column),
    derivation correctly computes per-unit price.
    """
    price, is_derived = derive_unit_price(
        quantity_raw="100",
        unit_price_raw="771.66",
        line_total_raw="771.66"
    )
    assert is_derived is True
    assert price == "7.7166"
