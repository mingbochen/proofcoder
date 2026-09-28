import unittest

from storefront.errors import InsufficientStockError
from storefront.inventory.reservations import best_warehouse, reserve
from storefront.inventory.stock import StockLedger


def _ledger() -> StockLedger:
    ledger = StockLedger()
    ledger.receive("lyon", "GM-010", 2)
    ledger.receive("lille", "GM-010", 5)
    ledger.receive("lille", "ST-100", 40)
    return ledger


class StockTests(unittest.TestCase):
    def test_levels_and_totals(self) -> None:
        ledger = _ledger()
        self.assertEqual(ledger.level("lyon", "GM-010"), 2)
        self.assertEqual(ledger.level("lyon", "ST-100"), 0)
        self.assertEqual(ledger.total("GM-010"), 7)

    def test_receiving_nothing_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StockLedger().receive("lyon", "GM-010", 0)


class ReservationTests(unittest.TestCase):
    def test_reserve_takes_stock(self) -> None:
        ledger = _ledger()
        reserve(ledger, "lille", "GM-010", 3)
        self.assertEqual(ledger.level("lille", "GM-010"), 2)

    def test_failed_reservation_takes_nothing(self) -> None:
        ledger = _ledger()
        with self.assertRaises(InsufficientStockError):
            reserve(ledger, "lyon", "GM-010", 3)
        self.assertEqual(ledger.level("lyon", "GM-010"), 2)

    def test_best_warehouse_keeps_the_given_order(self) -> None:
        ledger = _ledger()
        self.assertEqual(best_warehouse(ledger, ["lyon", "lille"], "GM-010", 2), "lyon")
        self.assertEqual(best_warehouse(ledger, ["lyon", "lille"], "GM-010", 4), "lille")
        self.assertIsNone(best_warehouse(ledger, ["lyon", "lille"], "GM-010", 6))


if __name__ == "__main__":
    unittest.main()
