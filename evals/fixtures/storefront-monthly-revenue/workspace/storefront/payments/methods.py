"""Accepted payment methods and what each costs to process."""

from enum import StrEnum

from storefront.money import percent_of


class PaymentMethod(StrEnum):
    CARD = "card"
    TRANSFER = "transfer"
    VOUCHER = "voucher"


_FEE_PERCENT = {PaymentMethod.CARD: 2, PaymentMethod.TRANSFER: 0, PaymentMethod.VOUCHER: 0}


def processing_fee_cents(method: PaymentMethod, amount_cents: int) -> int:
    return percent_of(amount_cents, _FEE_PERCENT[method])
