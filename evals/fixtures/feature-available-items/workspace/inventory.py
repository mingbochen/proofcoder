"""Tiny inventory helpers used by the feature fixture."""


def total_units(items: list[tuple[str, int]]) -> int:
    """Return the number of units across all item records."""

    return sum(count for _, count in items)


def available_names(items: list[tuple[str, int]]) -> list[str]:
    """Return item names in input order."""

    return [name for name, _ in items]
