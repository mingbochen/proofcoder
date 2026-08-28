"""Offline protocol tests for the DeepSeek client."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from proofcoder.config import ProofCoderConfig
from proofcoder.errors import ConfigurationError, DeepSeekAPIError, LLMErrorCategory
from proofcoder.llm.deepseek import DeepSeekClient

SENSITIVE_SENTINEL = "never-print-this-value"
REASONING_SENTINEL = "internal-reasoning-must-stay-private"


@dataclass
class _FakeMessage:
    content: str | None
    reasoning_content: str | None
    tool_calls: list[_FakeToolCall] | None = None


@dataclass
class _FakeToolFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    type: str
    function: _FakeToolFunction


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str | None


@dataclass
class _FakeUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]
    usage: _FakeUsage | None


class _FakeCompletions:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.request: dict[str, object] | None = None

    def create(self, **kwargs: object) -> object:
        self.request = kwargs
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@dataclass
class _FakeChat:
    completions: _FakeCompletions


@dataclass
class _FakeOpenAIClient:
    chat: _FakeChat


def _config() -> ProofCoderConfig:
    return ProofCoderConfig.from_env(environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL})


def _response() -> _FakeResponse:
    return _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content="OK",
                    reasoning_content=REASONING_SENTINEL,
                ),
                finish_reason="stop",
            )
        ],
        usage=_FakeUsage(prompt_tokens=5, completion_tokens=7, total_tokens=12),
    )


def test_openai_client_is_constructed_with_retries_disabled() -> None:
    captured: dict[str, object] = {}
    completions = _FakeCompletions(_response())

    def factory(**kwargs: object) -> _FakeOpenAIClient:
        captured.update(kwargs)
        return _FakeOpenAIClient(_FakeChat(completions))

    DeepSeekClient(_config(), client_factory=factory)

    assert captured == {
        "api_key": SENSITIVE_SENTINEL,
        "base_url": "https://api.deepseek.com",
        "max_retries": 0,
    }


def test_request_contract_and_response_normalization() -> None:
    completions = _FakeCompletions(_response())
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(completions)),
    )

    result = client.complete([{"role": "user", "content": "hello"}])

    assert completions.request is not None
    assert completions.request["model"] == "deepseek-v4-flash"
    assert completions.request["stream"] is False
    assert completions.request["reasoning_effort"] == "high"
    assert completions.request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert completions.request["messages"] == [{"role": "user", "content": "hello"}]
    assert "tools" not in completions.request
    for forbidden in (
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
    ):
        assert forbidden not in completions.request

    assert result.content == "OK"
    assert result.reasoning_content == REASONING_SENTINEL
    assert result.finish_reason == "stop"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 5
    assert result.usage.completion_tokens == 7
    assert result.usage.total_tokens == 12


def test_tools_request_and_multiple_tool_calls_are_preserved() -> None:
    raw_arguments = '{ "path": ".", "max_depth": 1 }'
    response = _FakeResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content=None,
                    reasoning_content=REASONING_SENTINEL,
                    tool_calls=[
                        _FakeToolCall(
                            id="call-1",
                            type="function",
                            function=_FakeToolFunction(
                                name="list_files",
                                arguments=raw_arguments,
                            ),
                        ),
                        _FakeToolCall(
                            id="call-2",
                            type="function",
                            function=_FakeToolFunction(
                                name="list_files",
                                arguments="{}",
                            ),
                        ),
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=None,
    )
    completions = _FakeCompletions(response)
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(completions)),
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "list",
                "parameters": {"type": "object"},
            },
        }
    ]

    result = client.complete([{"role": "user", "content": "list"}], tools)

    assert completions.request is not None
    assert completions.request["tools"] == tools
    assert completions.request["max_tokens"] == 1024
    assert "strict" not in completions.request["tools"][0]["function"]
    assert [call.id for call in result.tool_calls] == ["call-1", "call-2"]
    assert result.tool_calls[0].function.name == "list_files"
    assert result.tool_calls[0].function.arguments == raw_arguments
    assert result.reasoning_content == REASONING_SENTINEL


def test_connectivity_check_uses_minimal_message() -> None:
    completions = _FakeCompletions(_response())
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(completions)),
    )

    client.check_connection()

    assert completions.request is not None
    assert completions.request["messages"] == [{"role": "user", "content": "Reply with OK."}]


def test_api_exception_is_converted_without_leaking_details() -> None:
    completions = _FakeCompletions(error=RuntimeError(SENSITIVE_SENTINEL))
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(completions)),
    )

    with pytest.raises(DeepSeekAPIError) as captured:
        client.check_connection()

    assert SENSITIVE_SENTINEL not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (400, LLMErrorCategory.BAD_REQUEST, False),
        (401, LLMErrorCategory.AUTHENTICATION, False),
        (402, LLMErrorCategory.PAYMENT_REQUIRED, False),
        (422, LLMErrorCategory.UNPROCESSABLE, False),
        (429, LLMErrorCategory.RATE_LIMIT, True),
        (500, LLMErrorCategory.SERVER, True),
        (503, LLMErrorCategory.SERVER, True),
        (502, LLMErrorCategory.PERMANENT, False),
    ],
)
def test_status_errors_are_classified_without_response_body(
    status: int,
    category: LLMErrorCategory,
    retryable: bool,
) -> None:
    request = httpx.Request("POST", "https://example.invalid/chat")
    response = httpx.Response(
        status,
        request=request,
        headers={"retry-after": "7", "x-request-id": "request-safe"},
        text=SENSITIVE_SENTINEL,
    )
    error = APIStatusError(SENSITIVE_SENTINEL, response=response, body=SENSITIVE_SENTINEL)
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(_FakeCompletions(error=error))),
    )

    with pytest.raises(DeepSeekAPIError) as captured:
        client.check_connection()

    assert captured.value.category is category
    assert captured.value.status_code == status
    assert captured.value.retryable is retryable
    assert captured.value.retry_after_seconds == 7
    assert captured.value.request_id == "request-safe"
    assert SENSITIVE_SENTINEL not in str(captured.value)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (
            APIConnectionError(
                message=SENSITIVE_SENTINEL,
                request=httpx.Request("POST", "https://example.invalid/chat"),
            ),
            LLMErrorCategory.CONNECTION,
        ),
        (
            APITimeoutError(httpx.Request("POST", "https://example.invalid/chat")),
            LLMErrorCategory.TIMEOUT,
        ),
    ],
)
def test_transport_errors_are_retryable_and_sanitized(
    error: Exception,
    category: LLMErrorCategory,
) -> None:
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(_FakeCompletions(error=error))),
    )

    with pytest.raises(DeepSeekAPIError) as captured:
        client.check_connection()

    assert captured.value.category is category
    assert captured.value.retryable is True
    assert SENSITIVE_SENTINEL not in str(captured.value)


def test_client_initialization_exception_is_safe() -> None:
    def factory(**kwargs: object) -> _FakeOpenAIClient:
        raise RuntimeError(f"{kwargs['api_key']} must not escape")

    with pytest.raises(DeepSeekAPIError) as captured:
        DeepSeekClient(_config(), client_factory=factory)

    assert SENSITIVE_SENTINEL not in str(captured.value)
    assert captured.value.__cause__ is None


def test_missing_api_key_cannot_create_client() -> None:
    config = ProofCoderConfig.from_env(offline=True, environ={})

    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY is required"):
        DeepSeekClient(config)


@pytest.mark.parametrize(
    "response",
    [
        {"choices": []},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "message": {"content": ["invalid"], "reasoning_content": None},
                    "finish_reason": "stop",
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {"content": "ok", "reasoning_content": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": "invalid"},
        },
    ],
)
def test_malformed_response_is_rejected(response: object) -> None:
    client = DeepSeekClient(
        _config(),
        client=_FakeOpenAIClient(_FakeChat(_FakeCompletions(response))),
    )

    with pytest.raises(DeepSeekAPIError, match="invalid response"):
        client.check_connection()
