import unittest

from storefront.reports.aggregation import Totals
from storefront.reports.export import HEADER, to_csv
from storefront.reports.monthly import MonthlyReport


class ExportTests(unittest.TestCase):
    def test_csv_has_a_header_and_one_row(self) -> None:
        report = MonthlyReport(
            "2026-02", Totals(orders=3, units=5, gross_cents=900, discount_cents=0)
        )
        self.assertEqual(
            to_csv(report).splitlines(),
            [",".join(HEADER), "2026-02,3,5,900,0,900"],
        )


if __name__ == "__main__":
    unittest.main()
