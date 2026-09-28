import unittest
from datetime import date

from storefront.catalog import Catalog, Category, Product
from storefront.errors import InvalidOrderError, UnknownProductError
from storefront.orders import Order, OrderLine, OrderStatus
from storefront.orders.builder import build_order
from storefront.orders.validation import validate


def _catalog() -> Catalog:
    catalog = Catalog()
    catalog.add(Product("GM-010", "Harbor Lights board game", Category.GAMES, 3_500))
    catalog.add(Product("ST-100", "Dot grid notebook", Category.STATIONERY, 650))
    return catalog


class BuilderTests(unittest.TestCase):
    def test_lines_take_the_catalog_price(self) -> None:
        order = build_order(_catalog(), "O-1", "C1", date(2026, 3, 2), [("GM-010", 2)])
        self.assertEqual(order.lines, [OrderLine("GM-010", 2, 3_500)])
        self.assertEqual(order.lines[0].subtotal_cents, 7_000)
        self.assertEqual(order.status, OrderStatus.PLACED)

    def test_item_count(self) -> None:
        items = [("GM-010", 2), ("ST-100", 5)]
        order = build_order(_catalog(), "O-1", "C1", date(2026, 3, 2), items)
        self.assertEqual(order.item_count(), 7)

    def test_unknown_product(self) -> None:
        with self.assertRaises(UnknownProductError):
            build_order(_catalog(), "O-1", "C1", date(2026, 3, 2), [("XX-1", 1)])


class ValidationTests(unittest.TestCase):
    def _order(self, lines: list[OrderLine]) -> Order:
        return Order("O-9", "C1", date(2026, 3, 2), lines=lines)

    def test_empty_order(self) -> None:
        with self.assertRaises(InvalidOrderError):
            validate(self._order([]), _catalog())

    def test_quantity_bounds(self) -> None:
        with self.assertRaises(InvalidOrderError):
            validate(self._order([OrderLine("GM-010", 0, 3_500)]), _catalog())
        with self.assertRaises(InvalidOrderError):
            validate(self._order([OrderLine("GM-010", 100, 3_500)]), _catalog())

    def test_duplicate_lines(self) -> None:
        line = OrderLine("ST-100", 1, 650)
        with self.assertRaises(InvalidOrderError):
            validate(self._order([line, line]), _catalog())


class StatusTests(unittest.TestCase):
    def test_allowed_moves(self) -> None:
        order = Order("O-1", "C1", date(2026, 3, 2))
        order.move_to(OrderStatus.PAID)
        order.move_to(OrderStatus.SHIPPED)
        self.assertEqual(order.status, OrderStatus.SHIPPED)

    def test_cancelled_is_final(self) -> None:
        order = Order("O-1", "C1", date(2026, 3, 2))
        order.move_to(OrderStatus.CANCELLED)
        with self.assertRaises(ValueError):
            order.move_to(OrderStatus.PAID)


if __name__ == "__main__":
    unittest.main()
