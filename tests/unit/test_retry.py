"""Deterministic retry-policy tests without real sleeping or network access."""

from __future__ import annotations

import pytest

from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.retry import MAX_RETRY_AFTER_SECONDS, retry_delay_seconds


@pytest.mark.parametrize(
    ("category", "status", "retryable"),
    [
        (LLMErrorCategory.CONNECTION, None, True),
        (LLMErrorCategory.TIMEOUT, None, True),
        (LLMErrorCategory.RATE_LIMIT, 429, True),
        (LLMErrorCategory.RATE_LIMIT, None, False),
        (LLMErrorCategory.SERVER, 500, True),
        (LLMErrorCategory.SERVER, 503, True),
        (LLMErrorCategory.SERVER, 502, False),
        (LLMErrorCategory.BAD_REQUEST, 400, False),
        (LLMErrorCategory.AUTHENTICATION, 401, False),
        (LLMErrorCategory.PAYMENT_REQUIRED, 402, False),
        (LLMErrorCategory.UNPROCESSABLE, 422, False),
        (LLMErrorCategory.PERMANENT, None, False),
    ],
)
def test_only_explicit_transient_categories_are_retryable(
    category: LLMErrorCategory,
    status: int | None,
    retryable: bool,
) -> None:
    error = LLMRequestError("safe", category=category, status_code=status)

    assert error.retryable is retryable


def test_delay_uses_exponential_backoff_and_deterministic_jitter() -> None:
    error = LLMRequestError("safe", category=LLMErrorCategory.CONNECTION)

    first = retry_delay_seconds(error, retry_number=1, random_value=0.0)
    second = retry_delay_seconds(error, retry_number=2, random_value=1.0)

    assert first == 0.5
    assert second == 1.25


def test_retry_after_is_respected_but_capped() -> None:
    error = LLMRequestError(
        "safe",
        category=LLMErrorCategory.RATE_LIMIT,
        status_code=429,
        retry_after_seconds=1000,
    )

    assert retry_delay_seconds(error, retry_number=1, random_value=0.0) == (MAX_RETRY_AFTER_SECONDS)


def test_sanitized_error_mapping_contains_no_message_or_provider_body() -> None:
    error = LLMRequestError(
        "private detail",
        category=LLMErrorCategory.RATE_LIMIT,
        status_code=429,
        retry_after_seconds=2.0,
        request_id="request-safe",
    )

    assert error.to_dict() == {
        "category": "rate_limit",
        "status_code": 429,
        "retry_after_seconds": 2.0,
        "request_id": "request-safe",
    }
    assert "private detail" not in str(error.to_dict())


@pytest.mark.parametrize("retry_number,random_value", [(0, 0.0), (1, -0.1), (1, 1.1)])
def test_invalid_retry_inputs_are_rejected(retry_number: int, random_value: float) -> None:
    error = LLMRequestError("safe", category=LLMErrorCategory.CONNECTION)

    with pytest.raises(ValueError):
        retry_delay_seconds(
            error,
            retry_number=retry_number,
            random_value=random_value,
        )
