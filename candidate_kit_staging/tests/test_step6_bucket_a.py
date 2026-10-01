import pytest
from bookable.ground import derive_unit_price

def test_bucket_a_lot_price_per_units():
    # DU-05 scenario: 100 units, unit_price extracted as 771.66 (lot price for 100), total is 771.66
    price, derived = derive_unit_price(quantity_raw="100", unit_price_raw="771.66", line_total_raw="771.66")
    assert derived is True
    assert price == "7.7166"

def test_bucket_a_normal_unit_price_preserved():
    # Normal case: 2 units at 10.00 = 20.00
    price, derived = derive_unit_price(quantity_raw="2", unit_price_raw="10.00", line_total_raw="20.00")
    assert derived is False
    assert price == "10.00"

def test_bucket_a_strict_derivation_missing_quantity():
    # Per Step 6 Requirement 6: missing quantity should NOT default to 1, should not derive
    price, derived = derive_unit_price(quantity_raw=None, unit_price_raw=None, line_total_raw="50.00")
    assert derived is False
    assert price is None

def test_bucket_a_strict_derivation_missing_total():
    # Missing total should not derive
    price, derived = derive_unit_price(quantity_raw="5", unit_price_raw=None, line_total_raw=None)
    assert derived is False
    assert price is None

def test_bucket_a_valid_derivation():
    # Both quantity and total present, price missing
    price, derived = derive_unit_price(quantity_raw="4", unit_price_raw=None, line_total_raw="100.00")
    assert derived is True
    assert price == "25"
