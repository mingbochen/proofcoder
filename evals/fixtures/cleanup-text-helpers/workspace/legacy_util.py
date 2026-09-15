"""Superseded copy of the text helpers that no module imports any more."""

from __future__ import annotations


def slugify(title: str) -> str:
    """Return *title* as a lowercase, underscore-separated slug."""

    return "_".join(title.lower().split())
