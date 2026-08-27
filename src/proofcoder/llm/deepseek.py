"""Minimal DeepSeek Chat Completions client for Stage A."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, cast

from openai import OpenAI

from proofcoder.config import ProofCoderConfig
from proofcoder.errors import ConfigurationError, DeepSeekAPIError
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
                )
            except Exception:
                raise DeepSeekAPIError(
                    "DeepSeek API client initialization failed; check the endpoint configuration."
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
            "max_tokens": 1024 if tools else 16,
        }
        if tools:
            request["tools"] = list(tools)
        try:
            response = self._client.chat.completions.create(**request)
        except Exception:
            raise DeepSeekAPIError(
                "DeepSeek API request failed; check credentials, endpoint, and network."
            ) from None

        return _normalize_response(response)

    def check_connection(self) -> ModelResponse:
        """Issue the minimal request used by the online doctor command."""

        return self.complete(({"role": "user", "content": "Reply with OK."},))


def _normalize_response(response: object) -> ModelResponse:
    choices = _field(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise DeepSeekAPIError("DeepSeek API returned an invalid response.")

    choice = choices[0]
    message = _field(choice, "message")
    if message is None:
        raise DeepSeekAPIError("DeepSeek API returned an invalid response.")

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
        raise DeepSeekAPIError("DeepSeek API returned an invalid response.")

    normalized: list[ToolCall] = []
    for raw_call in value:
        function = _field(raw_call, "function")
        if function is None:
            raise DeepSeekAPIError("DeepSeek API returned an invalid response.")
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
    raise DeepSeekAPIError("DeepSeek API returned an invalid response.")


def _required_text(value: object | None) -> str:
    result = _optional_text(value)
    if result is None:
        raise DeepSeekAPIError("DeepSeek API returned an invalid response.")
    return result


def _optional_int(value: object | None) -> int | None:
    if value is None or isinstance(value, int):
        return value
    raise DeepSeekAPIError("DeepSeek API returned an invalid response.")
