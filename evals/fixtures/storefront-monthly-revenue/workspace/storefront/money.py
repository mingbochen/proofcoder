"""Money is kept in integer cents so that sums never drift."""

from storefront.config import CURRENCY


def to_cents(amount: str) -> int:
    """Parse a decimal string such as ``"12.50"`` into cents."""

    whole, _, fraction = amount.strip().partition(".")
    if len(fraction) > 2:
        raise ValueError(f"too many decimal places in {amount!r}")
    sign = -1 if whole.startswith("-") else 1
    whole_digits = whole.lstrip("-") or "0"
    return sign * (int(whole_digits) * 100 + int(fraction.ljust(2, "0")))


def format_cents(cents: int) -> str:
    """Format cents for display, for example ``1250`` as ``"12.50 EUR"``."""

    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return f"{sign}{whole}.{fraction:02d} {CURRENCY}"


def percent_of(cents: int, percent: int) -> int:
    """Return ``percent`` percent of ``cents``, rounded down to a whole cent."""

    return cents * percent // 100
