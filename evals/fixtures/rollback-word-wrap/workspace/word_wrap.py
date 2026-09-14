"""Wrap text onto lines of a bounded width."""

from __future__ import annotations


def wrap_words(text: str, width: int) -> list[str]:
    """Return *text* split into lines no longer than *width* characters."""

    if width < 1:
        raise ValueError("width must be at least 1")
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else f"{current} {word}"
        if len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if lines:
        lines.append(current)
    return lines
