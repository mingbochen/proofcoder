"""Text helpers for search and report layout."""

import unicodedata


def normalize(text: str) -> str:
    """Lower-case, strip accents and collapse whitespace."""

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.lower().split())


def pad_columns(cells: list[str], widths: list[int]) -> str:
    """Left-align each cell in its column and join the columns with two spaces."""

    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()
