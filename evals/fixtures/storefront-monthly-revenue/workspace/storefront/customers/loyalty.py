"""Loyalty points are earned on what a customer actually paid."""

from storefront.config import LOYALTY_POINTS_PER_EURO
from storefront.customers.customer import Customer


def points_for(paid_cents: int) -> int:
    """Return the points earned for a payment; partial euros earn nothing."""

    return max(0, paid_cents // 100) * LOYALTY_POINTS_PER_EURO


def award(customer: Customer, paid_cents: int) -> int:
    earned = points_for(paid_cents)
    customer.loyalty_points += earned
    return earned


def redeem(customer: Customer, points: int) -> None:
    if points < 0 or points > customer.loyalty_points:
        raise ValueError("cannot redeem more points than the customer holds")
    customer.loyalty_points -= points
