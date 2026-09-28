"""Sum priced lines into totals. Knows nothing about months or layout."""

from dataclasses import dataclass

from storefront.catalog.products import Catalog
from storefront.orders.order import Order
from storefront.pricing.lines import PricedLine, price_line
from storefront.pricing.promotions import PromotionBook


@dataclass(frozen=True)
class Totals:
    orders: int
    units: int
    gross_cents: int
    discount_cents: int

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.discount_cents


def priced_lines(order: Order, catalog: Catalog, promotions: PromotionBook) -> list[PricedLine]:
    return [price_line(line, catalog, promotions) for line in order.lines]


def total(orders: list[Order], catalog: Catalog, promotions: PromotionBook) -> Totals:
    lines = [priced for order in orders for priced in priced_lines(order, catalog, promotions)]
    return Totals(
        orders=len(orders),
        units=sum(line.quantity for line in lines),
        gross_cents=sum(line.gross_cents for line in lines),
        discount_cents=sum(line.discount_cents for line in lines),
    )
