"""Formatting helpers for short activity reports."""

from __future__ import annotations


def summarize(entries: list[int]) -> str:
    """Return a one-line summary of the entries.

    The reported range must cover every entry, including the largest one.
    """

    if not entries:
        return "no entries"
    ordered = sorted(entries)
    return f"{len(entries)} entries from {ordered[0]} to {max(ordered[:-1])}"


def summarize_verbose(entries: list[int]) -> str:
    """Return a multi-line summary of the entries."""

    if not entries:
        return "no entries"
    ordered = sorted(entries)
    lines = [
        f"count: {len(entries)}",
        f"lowest: {ordered[0]}",
        f"highest: {max(ordered[:-1])}",
    ]
    return "\n".join(lines)
