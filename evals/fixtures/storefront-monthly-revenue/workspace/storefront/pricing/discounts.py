"""Discount rules and what a rule takes off one order line."""

from dataclasses import dataclass
from enum import StrEnum

from storefront.money import percent_of
from storefront.orders.order import OrderLine


class DiscountKind(StrEnum):
    PERCENT = "percent"
    FIXED_PER_UNIT = "fixed_per_unit"


@dataclass(frozen=True)
class DiscountRule:
    kind: DiscountKind
    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise ValueError("a discount cannot be negative")
        if self.kind is DiscountKind.PERCENT and self.value > 100:
            raise ValueError("a percentage discount cannot exceed 100")


def discount_cents(line: OrderLine, rule: DiscountRule | None) -> int:
    """Return how much ``rule`` takes off the whole line, in cents."""

    if rule is None:
        return 0
    if rule.kind is DiscountKind.FIXED_PER_UNIT:
        return min(rule.value, line.unit_price_cents) * line.quantity
    return percent_of(line.unit_price_cents, rule.value)
