import unittest
from datetime import date

from storefront.utils.dates import in_month, month_key, parse_month
from storefront.utils.text import normalize, pad_columns


class DateTests(unittest.TestCase):
    def test_month_key(self) -> None:
        self.assertEqual(month_key(date(2026, 3, 9)), "2026-03")

    def test_parse_month(self) -> None:
        self.assertEqual(parse_month("2026-11"), (2026, 11))
        with self.assertRaises(ValueError):
            parse_month("2026-13")

    def test_in_month(self) -> None:
        self.assertTrue(in_month(date(2026, 3, 31), "2026-03"))
        self.assertFalse(in_month(date(2026, 4, 1), "2026-03"))


class TextTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(normalize("  Café   Crème "), "cafe creme")

    def test_pad_columns(self) -> None:
        self.assertEqual(pad_columns(["a", "bb"], [3, 4]), "a    bb")


if __name__ == "__main__":
    unittest.main()
