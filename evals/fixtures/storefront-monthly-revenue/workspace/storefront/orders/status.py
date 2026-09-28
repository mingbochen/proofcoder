"""Order lifecycle."""

from enum import StrEnum


class OrderStatus(StrEnum):
    PLACED = "placed"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


_ALLOWED = {
    OrderStatus.PLACED: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.SHIPPED, OrderStatus.REFUNDED},
    OrderStatus.SHIPPED: {OrderStatus.REFUNDED},
    OrderStatus.CANCELLED: set(),
    OrderStatus.REFUNDED: set(),
}


def can_move(current: OrderStatus, target: OrderStatus) -> bool:
    return target in _ALLOWED[current]


def counts_as_revenue(status: OrderStatus) -> bool:
    """Only paid or shipped orders count towards revenue."""

    return status in {OrderStatus.PAID, OrderStatus.SHIPPED}
