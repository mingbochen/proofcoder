"""Product categories. Promotions and tax rates are attached to categories."""

from enum import StrEnum


class Category(StrEnum):
    BOOKS = "books"
    GAMES = "games"
    STATIONERY = "stationery"
    TOYS = "toys"


def parse_category(value: str) -> Category:
    """Parse a category name case-insensitively."""

    return Category(value.strip().lower())
