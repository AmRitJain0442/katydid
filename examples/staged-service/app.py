"""Small dependency-free order service used by stage orchestration acceptance."""


def quote(subtotal_cents: int, item_count: int) -> dict[str, int]:
    if subtotal_cents < 0:
        raise ValueError("subtotal_cents cannot be negative")
    if item_count < 1:
        raise ValueError("item_count must be positive")
    discount = subtotal_cents // 10 if item_count >= 5 else 0
    taxable = subtotal_cents - discount
    tax = (taxable * 18 + 50) // 100
    return {
        "subtotal": subtotal_cents,
        "discount": discount,
        "tax": tax,
        "total": taxable + tax,
    }
