"""Value-added tax. Catalog prices exclude it."""

from storefront.catalog.categories import Category
from storefront.config import DEFAULT_TAX_RATE_PERCENT
from storefront.money import percent_of

REDUCED_RATES_PERCENT = {Category.BOOKS: 5}


def tax_rate_for(category: Category) -> int:
    return REDUCED_RATES_PERCENT.get(category, DEFAULT_TAX_RATE_PERCENT)


def tax_cents(net_cents: int, category: Category) -> int:
    return percent_of(net_cents, tax_rate_for(category))
