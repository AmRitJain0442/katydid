from decimal import Decimal

import pytest
from pricing import total


def test_discount_applies_to_the_entire_order():
    assert total(Decimal("12.50"), 3, Decimal("0.20")) == Decimal("30.00")


def test_rounds_half_up_to_cents():
    assert total(Decimal("1.005"), 1) == Decimal("1.01")


@pytest.mark.parametrize("quantity", [0, -1, True])
def test_invalid_quantities_are_rejected(quantity):
    with pytest.raises(ValueError):
        total(Decimal("12.50"), quantity)


@pytest.mark.parametrize("discount", ["-0.1", "1.1", "NaN"])
def test_invalid_discounts_are_rejected(discount):
    with pytest.raises(ValueError):
        total(Decimal("12.50"), 1, Decimal(discount))
