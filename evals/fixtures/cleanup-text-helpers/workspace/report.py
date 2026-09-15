"""Build the anchors and headings of a generated report."""

from __future__ import annotations

from util import slugify, titleize


def anchor(title: str) -> str:
    """Return the in-page anchor that links to *title*."""

    return f"#{slugify(title)}"


def heading(slug: str) -> str:
    """Return the display heading that *slug* was derived from."""

    return titleize(slug)
