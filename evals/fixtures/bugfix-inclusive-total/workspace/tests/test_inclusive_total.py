import unittest

from inclusive_total import inclusive_total


class InclusiveTotalTests(unittest.TestCase):
    def test_includes_upper_bound(self) -> None:
        self.assertEqual(inclusive_total(2, 4), 9)

    def test_descending_range_is_empty(self) -> None:
        self.assertEqual(inclusive_total(4, 2), 0)


if __name__ == "__main__":
    unittest.main()
