import unittest

from inventory import available_names, total_units


class InventoryTests(unittest.TestCase):
    def test_total_units(self) -> None:
        self.assertEqual(total_units([("pen", 2), ("eraser", 0), ("paper", 3)]), 5)

    def test_omits_out_of_stock_items(self) -> None:
        items = [("pen", 2), ("eraser", 0), ("paper", 3)]
        self.assertEqual(available_names(items), ["pen", "paper"])


if __name__ == "__main__":
    unittest.main()
