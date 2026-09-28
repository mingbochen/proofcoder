"""Refunds return whole lines of a paid or shipped order."""

from storefront.orders.order import Order
from storefront.orders.status import OrderStatus


def refundable(order: Order) -> bool:
    return order.status in {OrderStatus.PAID, OrderStatus.SHIPPED}


def refund_lines(order: Order, skus: set[str]) -> list[str]:
    """Return the skus that will be refunded, in the order's line order."""

    if not refundable(order):
        raise ValueError(f"{order.order_id} cannot be refunded while {order.status}")
    unknown = skus - {line.sku for line in order.lines}
    if unknown:
        raise ValueError(f"{order.order_id} has no line for {sorted(unknown)}")
    return [line.sku for line in order.lines if line.sku in skus]
