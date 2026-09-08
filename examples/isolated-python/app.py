"""Small dependency-free order allocation domain used by the isolated fixture."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Allocation:
    status: str
    total_units: int
    remaining: dict[str, int]
    reason: str | None = None


def allocate(stock: dict[str, int], request: dict[str, int]) -> Allocation:
    """Allocate a whole request or reject it without changing caller-owned stock."""
    if not request or any(not sku or units <= 0 for sku, units in request.items()):
        raise ValueError("request needs positive units for every SKU")
    if any(units < 0 for units in stock.values()):
        raise ValueError("stock cannot be negative")
    remaining = dict(stock)
    for sku, units in request.items():
        if remaining.get(sku, 0) < units:
            return Allocation("rejected", 0, dict(stock), f"insufficient:{sku}")
        remaining[sku] -= units
    return Allocation("confirmed", sum(request.values()), remaining)
