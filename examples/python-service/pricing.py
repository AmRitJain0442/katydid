"""Small local test target; no network service or payment operations."""

from decimal import ROUND_HALF_UP, Decimal


def total(unit_price: Decimal, quantity: int, discount: Decimal = Decimal("0")) -> Decimal:
    if not unit_price.is_finite() or unit_price < 0:
        raise ValueError("Unit price must be finite and nonnegative")
    if type(quantity) is not int or quantity < 1:
        raise ValueError("Quantity must be a positive integer")
    if not discount.is_finite() or not Decimal("0") <= discount <= Decimal("1"):
        raise ValueError("Discount must be between zero and one")
    return (unit_price * quantity * (1 - discount)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
