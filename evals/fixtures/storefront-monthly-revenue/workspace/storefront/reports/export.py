"""Export a monthly report as CSV for the accounting system."""

import csv
import io

from storefront.reports.monthly import MonthlyReport

HEADER = ["month", "orders", "units", "gross_cents", "discount_cents", "net_cents"]


def to_csv(report: MonthlyReport) -> str:
    totals = report.totals
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerow(
        [
            report.month,
            totals.orders,
            totals.units,
            totals.gross_cents,
            totals.discount_cents,
            report.net_revenue_cents,
        ]
    )
    return buffer.getvalue()
