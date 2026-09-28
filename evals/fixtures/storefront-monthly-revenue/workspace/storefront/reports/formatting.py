"""Render a monthly report as aligned text."""

from storefront.money import format_cents
from storefront.reports.monthly import MonthlyReport
from storefront.utils.text import pad_columns

_WIDTHS = [12, 14]


def render(report: MonthlyReport) -> str:
    totals = report.totals
    rows = [
        ["Month", report.month],
        ["Orders", str(totals.orders)],
        ["Units", str(totals.units)],
        ["Gross", format_cents(totals.gross_cents)],
        ["Discounts", format_cents(-totals.discount_cents)],
        ["Net revenue", format_cents(report.net_revenue_cents)],
    ]
    return "\n".join(pad_columns(row, _WIDTHS) for row in rows)
