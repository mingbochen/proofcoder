"""Build orders from catalog prices, so a line keeps the price it was ordered at."""

from datetime import date

from storefront.catalog.products import Catalog
from storefront.orders.order import Order, OrderLine
from storefront.orders.status import OrderStatus
from storefront.orders.validation import validate


def build_order(
    catalog: Catalog,
    order_id: str,
    customer_id: str,
    placed_on: date,
    items: list[tuple[str, int]],
    status: OrderStatus = OrderStatus.PLACED,
) -> Order:
    """Create a validated order from ``(sku, quantity)`` pairs."""

    lines = [
        OrderLine(sku=sku, quantity=quantity, unit_price_cents=catalog.get(sku).unit_price_cents)
        for sku, quantity in items
    ]
    order = Order(
        order_id=order_id,
        customer_id=customer_id,
        placed_on=placed_on,
        lines=lines,
        status=status,
    )
    validate(order, catalog)
    return order
