"""Shipping is flat below a threshold and free above it."""

from storefront.config import FLAT_SHIPPING_CENTS, FREE_SHIPPING_THRESHOLD_CENTS
from storefront.customers.addresses import Address

INTERNATIONAL_SURCHARGE_CENTS = 1_000


def shipping_cents(goods_cents: int, address: Address) -> int:
    base = 0 if goods_cents >= FREE_SHIPPING_THRESHOLD_CENTS else FLAT_SHIPPING_CENTS
    if not address.is_domestic():
        base += INTERNATIONAL_SURCHARGE_CENTS
    return base
