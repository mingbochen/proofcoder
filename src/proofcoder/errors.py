"""Project-specific exceptions with safe user-facing messages."""

from __future__ import annotations

from enum import StrEnum


class ProofCoderError(Exception):
    """Base class for recoverable ProofCoder failures."""


class ConfigurationError(ProofCoderError):
    """Raised when process environment configuration is invalid."""


class LLMErrorCategory(StrEnum):
    """Stable model-request error categories safe to expose and retry locally."""

    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    BAD_REQUEST = "bad_request"
    AUTHENTICATION = "authentication"
    PAYMENT_REQUIRED = "payment_required"
    UNPROCESSABLE = "unprocessable"
    INVALID_RESPONSE = "invalid_response"
    PERMANENT = "permanent"


class LLMRequestError(ProofCoderError):
    """A sanitized provider failure with only locally useful metadata."""

    def __init__(
        self,
        message: str,
        *,
        category: LLMErrorCategory = LLMErrorCategory.PERMANENT,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        """Return whether Stage D2 permits retrying this failure."""

        if self.category in {LLMErrorCategory.CONNECTION, LLMErrorCategory.TIMEOUT}:
            return True
        if self.category is LLMErrorCategory.RATE_LIMIT:
            return self.status_code == 429
        return self.category is LLMErrorCategory.SERVER and self.status_code in {500, 503}

    def to_dict(self) -> dict[str, object]:
        """Return only sanitized metadata suitable for local diagnostics."""

        payload: dict[str, object] = {
            "category": self.category.value,
            "status_code": self.status_code,
            "retry_after_seconds": self.retry_after_seconds,
        }
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        return payload


class DeepSeekAPIError(LLMRequestError):
    """Raised when a DeepSeek request or response cannot be handled safely."""


class MessageHistoryError(ProofCoderError):
    """Raised when a message would violate the local chat protocol."""


class ScriptedClientError(LLMRequestError):
    """Raised when an offline scripted response sequence cannot continue."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category=LLMErrorCategory.PERMANENT)


class ContextBudgetError(ProofCoderError):
    """Raised when required request context cannot fit the configured budget."""


class ToolRegistrationError(ProofCoderError):
    """Raised when a tool definition cannot be registered safely."""
