"""Price one order line: what it costs, and what its promotion takes off."""

from dataclasses import dataclass

from storefront.catalog.products import Catalog
from storefront.orders.order import OrderLine
from storefront.pricing.discounts import discount_cents
from storefront.pricing.promotions import PromotionBook


@dataclass(frozen=True)
class PricedLine:
    sku: str
    quantity: int
    gross_cents: int
    discount_cents: int

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.discount_cents


def price_line(line: OrderLine, catalog: Catalog, promotions: PromotionBook) -> PricedLine:
    rule = promotions.rule_for(catalog, line.sku)
    return PricedLine(
        sku=line.sku,
        quantity=line.quantity,
        gross_cents=line.subtotal_cents,
        discount_cents=discount_cents(line, rule),
    )
