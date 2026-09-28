"""An order is a dated list of lines placed by one customer."""

from dataclasses import dataclass, field
from datetime import date

from storefront.orders.status import OrderStatus, can_move


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price_cents: int

    @property
    def subtotal_cents(self) -> int:
        return self.unit_price_cents * self.quantity


@dataclass
class Order:
    order_id: str
    customer_id: str
    placed_on: date
    lines: list[OrderLine] = field(default_factory=list)
    status: OrderStatus = OrderStatus.PLACED

    def move_to(self, target: OrderStatus) -> None:
        if not can_move(self.status, target):
            raise ValueError(f"cannot move {self.order_id} from {self.status} to {target}")
        self.status = target

    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)
