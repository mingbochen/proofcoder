"""Formatting behavior backed by local settings."""

from settings import DEFAULT_PREFIX


def format_message(message: str) -> str:
    """Add the configured prefix to one message."""

    return f"{DEFAULT_PREFIX}: {message}"
