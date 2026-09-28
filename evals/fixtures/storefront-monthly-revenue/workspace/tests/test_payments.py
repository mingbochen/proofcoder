import unittest
from datetime import date

from storefront.orders import Order, OrderLine, OrderStatus
from storefront.payments.methods import PaymentMethod, processing_fee_cents
from storefront.payments.refunds import refund_lines


def _paid_order() -> Order:
    lines = [OrderLine("GM-010", 1, 3_500), OrderLine("ST-100", 2, 650)]
    return Order("O-1", "C1", date(2026, 3, 2), lines=lines, status=OrderStatus.PAID)


class FeeTests(unittest.TestCase):
    def test_card_costs_two_percent(self) -> None:
        self.assertEqual(processing_fee_cents(PaymentMethod.CARD, 10_000), 200)

    def test_transfer_is_free(self) -> None:
        self.assertEqual(processing_fee_cents(PaymentMethod.TRANSFER, 10_000), 0)


class RefundTests(unittest.TestCase):
    def test_refunds_keep_line_order(self) -> None:
        self.assertEqual(refund_lines(_paid_order(), {"ST-100", "GM-010"}), ["GM-010", "ST-100"])

    def test_unpaid_orders_cannot_be_refunded(self) -> None:
        order = _paid_order()
        order.status = OrderStatus.PLACED
        with self.assertRaises(ValueError):
            refund_lines(order, {"GM-010"})

    def test_unknown_lines_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            refund_lines(_paid_order(), {"TY-200"})


if __name__ == "__main__":
    unittest.main()
