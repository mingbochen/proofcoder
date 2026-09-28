import unittest
from datetime import date

from storefront.catalog import Catalog, Category, Product
from storefront.orders import Order, OrderStatus
from storefront.orders.builder import build_order
from storefront.pricing.promotions import PromotionBook
from storefront.reports.formatting import render
from storefront.reports.monthly import MonthlyReport, build_monthly_report


def _catalog() -> Catalog:
    catalog = Catalog()
    catalog.add(Product("BK-001", "Field Guide to Ferns", Category.BOOKS, 2_400))
    catalog.add(Product("GM-010", "Harbor Lights board game", Category.GAMES, 3_500))
    catalog.add(Product("ST-100", "Dot grid notebook", Category.STATIONERY, 650))
    catalog.add(Product("TY-200", "Wooden train set", Category.TOYS, 4_200))
    return catalog


def _promotions() -> PromotionBook:
    return PromotionBook.from_spec({"games": "20%", "stationery": "0.50 per unit"})


def _orders(catalog: Catalog) -> list[Order]:
    return [
        build_order(
            catalog,
            "O-1",
            "C1",
            date(2026, 3, 2),
            [("GM-010", 3), ("ST-100", 4)],
            status=OrderStatus.PAID,
        ),
        build_order(
            catalog,
            "O-2",
            "C2",
            date(2026, 3, 15),
            [("BK-001", 1), ("GM-010", 1)],
            status=OrderStatus.SHIPPED,
        ),
        build_order(
            catalog,
            "O-3",
            "C3",
            date(2026, 3, 20),
            [("TY-200", 2)],
            status=OrderStatus.CANCELLED,
        ),
        build_order(catalog, "O-4", "C1", date(2026, 3, 28), [("BK-001", 5)]),
        build_order(
            catalog,
            "O-5",
            "C2",
            date(2026, 4, 1),
            [("GM-010", 2)],
            status=OrderStatus.PAID,
        ),
    ]


def _march() -> MonthlyReport:
    catalog = _catalog()
    return build_monthly_report("2026-03", _orders(catalog), catalog, _promotions())


class MonthlyReportTests(unittest.TestCase):
    def test_only_paid_and_shipped_orders_of_the_month_count(self) -> None:
        report = _march()
        self.assertEqual(report.totals.orders, 2)
        self.assertEqual(report.totals.units, 9)

    def test_gross_revenue(self) -> None:
        self.assertEqual(_march().totals.gross_cents, 19_000)

    def test_net_revenue(self) -> None:
        self.assertEqual(
            _march().net_revenue_cents,
            16_000,
            "monthly net revenue is wrong for 2026-03",
        )

    def test_rendered_report(self) -> None:
        lines = render(_march()).splitlines()
        self.assertEqual(lines[0], "Month         2026-03")
        self.assertEqual(lines[-1], "Net revenue   160.00 EUR")


if __name__ == "__main__":
    unittest.main()
