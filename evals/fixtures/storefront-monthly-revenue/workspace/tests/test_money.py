import unittest

from storefront.money import format_cents, percent_of, to_cents


class MoneyTests(unittest.TestCase):
    def test_parses_decimal_strings(self) -> None:
        self.assertEqual(to_cents("12.5"), 1250)
        self.assertEqual(to_cents("7"), 700)
        self.assertEqual(to_cents("-3.05"), -305)
        self.assertEqual(to_cents(" 0.99 "), 99)

    def test_rejects_fractions_of_a_cent(self) -> None:
        with self.assertRaises(ValueError):
            to_cents("1.234")

    def test_formats_cents(self) -> None:
        self.assertEqual(format_cents(1250), "12.50 EUR")
        self.assertEqual(format_cents(-5), "-0.05 EUR")
        self.assertEqual(format_cents(0), "0.00 EUR")

    def test_percent_rounds_down(self) -> None:
        self.assertEqual(percent_of(999, 10), 99)
        self.assertEqual(percent_of(5_000, 20), 1_000)


if __name__ == "__main__":
    unittest.main()
