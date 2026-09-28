"""The monthly sales report."""

from dataclasses import dataclass

from storefront.catalog.products import Catalog
from storefront.orders.order import Order
from storefront.orders.status import counts_as_revenue
from storefront.pricing.promotions import PromotionBook
from storefront.reports.aggregation import Totals, total
from storefront.utils.dates import in_month


@dataclass(frozen=True)
class MonthlyReport:
    month: str
    totals: Totals

    @property
    def net_revenue_cents(self) -> int:
        return self.totals.net_cents


def build_monthly_report(
    month: str,
    orders: list[Order],
    catalog: Catalog,
    promotions: PromotionBook,
) -> MonthlyReport:
    """Report the orders placed in ``month`` that count as revenue."""

    selected = [
        order
        for order in orders
        if in_month(order.placed_on, month) and counts_as_revenue(order.status)
    ]
    return MonthlyReport(month=month, totals=total(selected, catalog, promotions))
