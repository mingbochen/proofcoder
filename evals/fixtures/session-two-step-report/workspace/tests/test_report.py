import unittest

from report import summarize, summarize_verbose


class SummarizeTests(unittest.TestCase):
    def test_range_covers_the_last_entry(self) -> None:
        self.assertEqual(
            summarize([3, 1, 2]),
            "3 entries from 1 to 3",
            "summarize must cover the last entry",
        )

    def test_empty(self) -> None:
        self.assertEqual(summarize([]), "no entries")


class SummarizeVerboseTests(unittest.TestCase):
    def test_highest_is_the_last_entry(self) -> None:
        self.assertEqual(
            summarize_verbose([3, 1, 2]),
            "count: 3\nlowest: 1\nhighest: 3",
            "summarize_verbose must report the last entry as the highest",
        )

    def test_empty(self) -> None:
        self.assertEqual(summarize_verbose([]), "no entries")


if __name__ == "__main__":
    unittest.main()
