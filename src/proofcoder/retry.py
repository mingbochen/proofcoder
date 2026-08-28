"""Deterministic local retry-delay policy for transient model API failures."""

from __future__ import annotations

from proofcoder.errors import LLMRequestError

DEFAULT_MAX_API_ATTEMPTS = 3
BASE_RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 30.0
RETRY_JITTER_RATIO = 0.25


def retry_delay_seconds(
    error: LLMRequestError,
    *,
    retry_number: int,
    random_value: float,
) -> float:
    """Return capped exponential backoff with positive deterministic jitter."""

    if retry_number < 1:
        raise ValueError("retry number must be positive")
    if not 0 <= random_value <= 1:
        raise ValueError("random value must be between zero and one")
    exponential = min(
        BASE_RETRY_DELAY_SECONDS * (2 ** (retry_number - 1)),
        MAX_RETRY_DELAY_SECONDS,
    )
    delay = exponential + exponential * RETRY_JITTER_RATIO * random_value
    retry_after = error.retry_after_seconds
    if retry_after is not None and retry_after >= 0:
        delay = max(delay, min(retry_after, MAX_RETRY_AFTER_SECONDS))
    return min(delay, MAX_RETRY_AFTER_SECONDS)
