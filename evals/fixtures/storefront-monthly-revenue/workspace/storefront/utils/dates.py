"""Date helpers for monthly reporting."""

from datetime import date


def month_key(day: date) -> str:
    """Return the ``YYYY-MM`` key a report groups by."""

    return f"{day.year:04d}-{day.month:02d}"


def parse_month(key: str) -> tuple[int, int]:
    """Parse a ``YYYY-MM`` key into a year and a month."""

    year_text, _, month_text = key.partition("-")
    year, month = int(year_text), int(month_text)
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month in {key!r}")
    return year, month


def in_month(day: date, key: str) -> bool:
    return month_key(day) == key
