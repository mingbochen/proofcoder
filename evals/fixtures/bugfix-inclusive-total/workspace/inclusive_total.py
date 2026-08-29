"""Small inclusive-range total helper."""


def inclusive_total(start: int, end: int) -> int:
    """Return the total of an inclusive integer interval."""

    if start > end:
        return 0
    return sum(range(start, end))
