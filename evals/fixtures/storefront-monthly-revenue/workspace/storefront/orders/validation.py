"""Rules an order must satisfy before it is accepted."""

from storefront.catalog.products import Catalog
from storefront.errors import InvalidOrderError
from storefront.orders.order import Order

MAX_LINES = 50
MAX_QUANTITY_PER_LINE = 99


def validate(order: Order, catalog: Catalog) -> None:
    if not order.lines:
        raise InvalidOrderError(f"{order.order_id} has no lines")
    if len(order.lines) > MAX_LINES:
        raise InvalidOrderError(f"{order.order_id} has more than {MAX_LINES} lines")
    seen: set[str] = set()
    for line in order.lines:
        if line.sku not in catalog:
            raise InvalidOrderError(f"{order.order_id} names unknown product {line.sku}")
        if not 1 <= line.quantity <= MAX_QUANTITY_PER_LINE:
            raise InvalidOrderError(f"{order.order_id} orders {line.quantity} of {line.sku}")
        if line.sku in seen:
            raise InvalidOrderError(f"{order.order_id} lists {line.sku} twice")
        seen.add(line.sku)
