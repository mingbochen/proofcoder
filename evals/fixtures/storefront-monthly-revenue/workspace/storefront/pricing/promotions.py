"""Which discount applies to which product, by category."""

from dataclasses import dataclass, field

from storefront.catalog.categories import Category, parse_category
from storefront.catalog.products import Catalog
from storefront.money import to_cents
from storefront.pricing.discounts import DiscountKind, DiscountRule


def parse_rule(text: str) -> DiscountRule:
    """Parse ``"20%"`` or ``"0.50 per unit"`` into a discount rule."""

    value = text.strip()
    if value.endswith("%"):
        return DiscountRule(DiscountKind.PERCENT, int(value[:-1]))
    amount, _, unit = value.partition(" per ")
    if unit.strip() != "unit":
        raise ValueError(f"unrecognized discount {text!r}")
    return DiscountRule(DiscountKind.FIXED_PER_UNIT, to_cents(amount))


@dataclass
class PromotionBook:
    _by_category: dict[Category, DiscountRule] = field(default_factory=dict)

    @classmethod
    def from_spec(cls, spec: dict[str, str]) -> "PromotionBook":
        """Build a book from a mapping such as ``{"games": "20%"}``."""

        book = cls()
        for category, text in spec.items():
            book.set(parse_category(category), parse_rule(text))
        return book

    def set(self, category: Category, rule: DiscountRule) -> None:
        self._by_category[category] = rule

    def clear(self, category: Category) -> None:
        self._by_category.pop(category, None)

    def rule_for(self, catalog: Catalog, sku: str) -> DiscountRule | None:
        return self._by_category.get(catalog.get(sku).category)
