"""Minimal DeepSeek Chat Completions client for Stage A."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Protocol, cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from proofcoder.config import ProofCoderConfig
from proofcoder.errors import ConfigurationError, DeepSeekAPIError, LLMErrorCategory
from proofcoder.llm.base import ChatMessagePayload, ToolSchema
from proofcoder.protocol import FunctionCall, ModelResponse, TokenUsage, ToolCall


class _CompletionsAPI(Protocol):
    def create(self, **kwargs: object) -> object: ...


class _ChatAPI(Protocol):
    completions: _CompletionsAPI


class _OpenAIClient(Protocol):
    chat: _ChatAPI


_ClientFactory = Callable[..., _OpenAIClient]
_DEFAULT_CLIENT_FACTORY = cast(_ClientFactory, OpenAI)

# deepseek-v4-flash documents a 384K maximum output length, so this ceiling is a
# project choice rather than a provider limit: large enough for a whole-file write,
# small enough that one runaway generation cannot consume the whole time budget.
MAX_TOOL_OUTPUT_TOKENS = 8192
MAX_CONNECTIVITY_OUTPUT_TOKENS = 16
# openai-python defaults to 600s, which equals AgentLoop's default max_seconds. Because
# the loop only checks elapsed time between requests, a single hung call would otherwise
# outlive the wall-clock guard entirely.
REQUEST_TIMEOUT_SECONDS = 120.0


class DeepSeekClient:
    """Send one synchronous, non-streaming request to DeepSeek."""

    def __init__(
        self,
        config: ProofCoderConfig,
        *,
        client: _OpenAIClient | None = None,
        client_factory: _ClientFactory = _DEFAULT_CLIENT_FACTORY,
    ) -> None:
        if config.api_key is None:
            raise ConfigurationError("DEEPSEEK_API_KEY is required to create the API client.")
        self._config = config
        if client is not None:
            self._client = client
        else:
            try:
                self._client = client_factory(
                    api_key=config.api_key,
                    base_url=config.base_url,
                    max_retries=0,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except Exception:
                raise DeepSeekAPIError(
                    "DeepSeek API client initialization failed; check the endpoint configuration.",
                    category=LLMErrorCategory.PERMANENT,
                ) from None

    def complete(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
    ) -> ModelResponse:
        """Make one Chat Completions request and normalize its response."""

        request: dict[str, object] = {
            "model": self._config.model,
            "messages": list(messages),
            "stream": False,
            "reasoning_effort": self._config.reasoning_effort,
            "extra_body": {"thinking": {"type": "enabled"}},
            "max_tokens": MAX_TOOL_OUTPUT_TOKENS if tools else MAX_CONNECTIVITY_OUTPUT_TOKENS,
        }
        if tools:
            request["tools"] = list(tools)
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as error:
            raise _classify_api_error(error) from None

        return _normalize_response(response)

    def check_connection(self) -> ModelResponse:
        """Issue the minimal request used by the online doctor command."""

        return self.complete(({"role": "user", "content": "Reply with OK."},))


def _normalize_response(response: object) -> ModelResponse:
    choices = _field(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise _invalid_response_error()

    choice = choices[0]
    message = _field(choice, "message")
    if message is None:
        raise _invalid_response_error()

    usage_value = _field(response, "usage")
    usage = None
    if usage_value is not None:
        usage = TokenUsage(
            prompt_tokens=_optional_int(_field(usage_value, "prompt_tokens")),
            completion_tokens=_optional_int(_field(usage_value, "completion_tokens")),
            total_tokens=_optional_int(_field(usage_value, "total_tokens")),
        )

    return ModelResponse(
        content=_optional_text(_field(message, "content")),
        reasoning_content=_optional_text(_field(message, "reasoning_content")),
        finish_reason=_optional_text(_field(choice, "finish_reason")),
        usage=usage,
        tool_calls=_normalize_tool_calls(_field(message, "tool_calls")),
    )


def _normalize_tool_calls(value: object | None) -> tuple[ToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise _invalid_response_error()

    normalized: list[ToolCall] = []
    for raw_call in value:
        function = _field(raw_call, "function")
        if function is None:
            raise _invalid_response_error()
        normalized.append(
            ToolCall(
                id=_required_text(_field(raw_call, "id")),
                type=_required_text(_field(raw_call, "type")),
                function=FunctionCall(
                    name=_required_text(_field(function, "name")),
                    arguments=_required_text(_field(function, "arguments")),
                ),
            )
        )
    return tuple(normalized)


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _optional_text(value: object | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise _invalid_response_error()


def _required_text(value: object | None) -> str:
    result = _optional_text(value)
    if result is None:
        raise _invalid_response_error()
    return result


def _optional_int(value: object | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    raise _invalid_response_error()


def _classify_api_error(error: Exception) -> DeepSeekAPIError:
    if isinstance(error, APITimeoutError):
        category = LLMErrorCategory.TIMEOUT
        status_code = None
    elif isinstance(error, APIConnectionError):
        category = LLMErrorCategory.CONNECTION
        status_code = None
    elif isinstance(error, APIStatusError):
        status_code = error.status_code
        category = {
            400: LLMErrorCategory.BAD_REQUEST,
            401: LLMErrorCategory.AUTHENTICATION,
            402: LLMErrorCategory.PAYMENT_REQUIRED,
            422: LLMErrorCategory.UNPROCESSABLE,
            429: LLMErrorCategory.RATE_LIMIT,
            500: LLMErrorCategory.SERVER,
            503: LLMErrorCategory.SERVER,
        }.get(status_code, LLMErrorCategory.PERMANENT)
    else:
        category = LLMErrorCategory.PERMANENT
        status_code = None
    return DeepSeekAPIError(
        f"DeepSeek API request failed ({category.value}).",
        category=category,
        status_code=status_code,
        retry_after_seconds=_retry_after_seconds(error),
        request_id=_request_id(error),
    )


def _invalid_response_error() -> DeepSeekAPIError:
    return DeepSeekAPIError(
        "DeepSeek API returned an invalid response.",
        category=LLMErrorCategory.INVALID_RESPONSE,
    )


def _retry_after_seconds(error: Exception) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("retry-after")
    except Exception:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    if seconds < 0 or not isfinite(seconds):
        return None
    return seconds


def _request_id(error: Exception) -> str | None:
    request_id = getattr(error, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id[:128]
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("x-request-id")
    except Exception:
        return None
    return value[:128] if isinstance(value, str) and value else None
