"""Text helpers shared by the reporting code."""

from __future__ import annotations


def slugify(title: str) -> str:
    """Return *title* as a lowercase, hyphen-separated slug."""

    return "-".join(title.lower().split())


def titleize(slug: str) -> str:
    """Return *slug* as a space-separated title."""

    return " ".join(word.capitalize() for word in slug.split("-") if word)
