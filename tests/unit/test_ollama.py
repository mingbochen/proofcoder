"""Offline tests for the local-model adapter and provider selection.

No socket is opened: the adapter is driven through a fake opener, so every conversion
and every failure classification is exercised deterministically.
"""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import Mapping
from typing import Any

import pytest

from proofcoder.config import ProofCoderConfig, ProviderName
from proofcoder.errors import ConfigurationError, LLMErrorCategory, LLMRequestError
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.llm.factory import create_client, create_connectivity_client
from proofcoder.llm.ollama import MAX_RESPONSE_BYTES, OllamaClient

SENSITIVE_SENTINEL = "never-send-this-to-a-local-model"


class _FakeOpener:
    """Return one canned HTTP body, or raise one canned error."""

    def __init__(self, body: object = None, *, error: Exception | None = None) -> None:
        self._body = body
        self._error = error
        self.requests: list[dict[str, Any]] = []

    def open(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(
            {
                "url": request.full_url,
                "method": request.method,
                "headers": dict(request.headers),
                "body": json.loads(request.data.decode("utf-8")),
            }
        )
        if self._error is not None:
            raise self._error
        raw = self._body if isinstance(self._body, bytes) else json.dumps(self._body).encode()
        return _FakeResponse(raw)


class _FakeResponse:
    def __init__(self, raw: bytes) -> None:
        self._stream = io.BytesIO(raw)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _config(**overrides: object) -> ProofCoderConfig:
    values: dict[str, Any] = {
        "api_key": None,
        "base_url": "http://localhost:11434",
        "model": "test-model",
        "provider": ProviderName.OLLAMA,
    }
    values.update(overrides)
    return ProofCoderConfig(**values)


def _client(body: object = None, *, error: Exception | None = None) -> tuple[OllamaClient, Any]:
    opener = _FakeOpener(body, error=error)
    return OllamaClient(_config(), opener=opener), opener


def _assistant(**fields: object) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": "hello"}
    message.update(fields)
    return {"model": "test-model", "message": message, "done": True, "done_reason": "stop"}


# ---------- configuration ----------


def test_the_default_provider_is_unchanged() -> None:
    config = ProofCoderConfig.from_env(environ={"DEEPSEEK_API_KEY": "x"})

    assert config.provider is ProviderName.DEEPSEEK
    assert config.requires_api_key is True
    assert config.base_url == "https://api.deepseek.com"


def test_the_local_provider_needs_no_credential_but_needs_a_model() -> None:
    config = ProofCoderConfig.from_env(
        environ={"PROOFCODER_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3:8b"}
    )

    assert config.provider is ProviderName.OLLAMA
    assert config.requires_api_key is False
    assert config.api_key is None
    assert config.model == "qwen3:8b"
    assert config.base_url == "http://localhost:11434"

    with pytest.raises(ConfigurationError) as missing:
        ProofCoderConfig.from_env(environ={"PROOFCODER_PROVIDER": "ollama"})
    assert "OLLAMA_MODEL" in str(missing.value)


def test_the_local_provider_ignores_the_other_providers_variables() -> None:
    """Each provider reads its own group; sharing names would hide which one applies."""

    config = ProofCoderConfig.from_env(
        environ={
            "PROOFCODER_PROVIDER": "ollama",
            "OLLAMA_MODEL": "qwen3:8b",
            "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
            "DEEPSEEK_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        }
    )

    assert config.api_key is None
    assert config.model == "qwen3:8b"
    assert config.base_url == "http://localhost:11434"


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ConfigurationError) as error:
        ProofCoderConfig.from_env(environ={"PROOFCODER_PROVIDER": "nope"})

    assert "PROOFCODER_PROVIDER" in str(error.value)


def test_a_trailing_slash_in_the_base_url_does_not_double_up() -> None:
    config = ProofCoderConfig.from_env(
        environ={
            "PROOFCODER_PROVIDER": "ollama",
            "OLLAMA_MODEL": "m",
            "OLLAMA_BASE_URL": "http://127.0.0.1:11434/",
        }
    )
    opener = _FakeOpener(_assistant())

    OllamaClient(config, opener=opener).complete(({"role": "user", "content": "hi"},))

    assert opener.requests[0]["url"] == "http://127.0.0.1:11434/api/chat"


def test_the_factory_selects_by_provider() -> None:
    local = create_client(_config())
    cloud = create_client(ProofCoderConfig(api_key="key", provider=ProviderName.DEEPSEEK))

    assert isinstance(local, OllamaClient)
    assert isinstance(cloud, DeepSeekClient)
    assert isinstance(create_connectivity_client(_config()), OllamaClient)


# ---------- request shape ----------


def test_the_request_is_non_streaming_and_carries_the_tools() -> None:
    client, opener = _client(_assistant())
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

    client.complete(({"role": "user", "content": "hi"},), tools)

    sent = opener.requests[0]
    assert sent["url"] == "http://localhost:11434/api/chat"
    assert sent["method"] == "POST"
    assert sent["body"]["stream"] is False
    assert sent["body"]["model"] == "test-model"
    assert sent["body"]["tools"] == tools


def test_a_tool_result_is_converted_to_the_providers_shape() -> None:
    """Ollama addresses a tool result by name, so the id stays out of the request."""

    client, opener = _client(_assistant())

    client.complete(
        (
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'},
        )
    )

    messages = opener.requests[0]["body"]["messages"]
    assert messages[1]["tool_calls"] == [
        {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
    ]
    assert messages[2] == {"role": "tool", "content": '{"ok": true}'}
    assert "tool_call_id" not in messages[2]
    assert "id" not in messages[1]["tool_calls"][0]


def test_unparsable_outgoing_arguments_become_an_empty_object() -> None:
    client, opener = _client(_assistant())

    client.complete(
        (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "not json"}}],
            },
        )
    )

    sent = opener.requests[0]["body"]["messages"][0]["tool_calls"][0]
    assert sent["function"]["arguments"] == {}


# ---------- response conversion ----------


def test_object_arguments_become_the_json_string_the_loop_expects() -> None:
    client, _ = _client(
        _assistant(
            content=None,
            tool_calls=[
                {"function": {"name": "read_file", "arguments": {"path": "a.py", "start": 1}}},
                {"function": {"name": "list_files", "arguments": {}}},
            ],
        )
    )

    response = client.complete(({"role": "user", "content": "hi"},))

    assert [call.function.name for call in response.tool_calls] == ["read_file", "list_files"]
    # A JSON string, not an object: the rest of ProofCoder carries arguments untouched.
    assert response.tool_calls[0].function.arguments == '{"path": "a.py", "start": 1}'
    assert json.loads(response.tool_calls[0].function.arguments) == {"path": "a.py", "start": 1}
    assert response.tool_calls[1].function.arguments == "{}"
    # Ollama issues no ids, so they are synthesized and distinct within one response.
    assert len({call.id for call in response.tool_calls}) == 2
    assert response.finish_reason == "tool_calls"


def test_thinking_and_usage_are_normalized() -> None:
    client, _ = _client(
        {
            "message": {"role": "assistant", "content": "done", "thinking": "private"},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 11,
            "eval_count": 5,
        }
    )

    response = client.complete(({"role": "user", "content": "hi"},))

    assert response.content == "done"
    assert response.reasoning_content == "private"
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (11, 5)
    assert response.usage.total_tokens == 16


def test_a_response_without_counts_reports_no_usage() -> None:
    client, _ = _client(_assistant())

    assert client.complete(({"role": "user", "content": "hi"},)).usage is None


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": None, "tool_calls": ["not a mapping"]},
        {"role": "assistant", "content": None, "tool_calls": [{"nofunction": 1}]},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": ""}}]},
    ],
)
def test_a_malformed_tool_call_is_an_invalid_response(message: Mapping[str, object]) -> None:
    client, _ = _client({"message": message, "done": True})

    with pytest.raises(LLMRequestError) as error:
        client.complete(({"role": "user", "content": "hi"},))

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE


def test_a_response_without_a_message_is_an_invalid_response() -> None:
    client, _ = _client({"done": True})

    with pytest.raises(LLMRequestError) as error:
        client.complete(({"role": "user", "content": "hi"},))

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE


@pytest.mark.parametrize("body", [b"not json", b"[]"])
def test_a_non_object_json_body_is_an_invalid_response(body: bytes) -> None:
    client, _ = _client(body)

    with pytest.raises(LLMRequestError) as error:
        client.complete(({"role": "user", "content": "hi"},))

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE


def test_an_oversized_body_is_refused_without_parsing() -> None:
    client, _ = _client(b"{" + b" " * (MAX_RESPONSE_BYTES + 8))

    with pytest.raises(LLMRequestError) as error:
        client.complete(({"role": "user", "content": "hi"},))

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE


# ---------- failure classification ----------


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (404, LLMErrorCategory.BAD_REQUEST, False),
        (400, LLMErrorCategory.BAD_REQUEST, False),
        (429, LLMErrorCategory.RATE_LIMIT, True),
        (500, LLMErrorCategory.SERVER, True),
        (503, LLMErrorCategory.SERVER, True),
        (418, LLMErrorCategory.BAD_REQUEST, False),
        (302, LLMErrorCategory.PERMANENT, False),
    ],
)
def test_http_statuses_map_onto_the_existing_categories(
    status: int,
    category: LLMErrorCategory,
    retryable: bool,
) -> None:
    error = urllib.error.HTTPError("http://x", status, "boom", {}, None)  # type: ignore[arg-type]
    client, _ = _client(error=error)

    with pytest.raises(LLMRequestError) as raised:
        client.complete(({"role": "user", "content": "hi"},))

    assert raised.value.category is category
    assert raised.value.retryable is retryable


def test_a_timeout_is_transient_and_an_unreachable_endpoint_is_a_connection_error() -> None:
    timed_out, _ = _client(error=TimeoutError())
    with pytest.raises(LLMRequestError) as first:
        timed_out.complete(({"role": "user", "content": "hi"},))
    assert first.value.category is LLMErrorCategory.TIMEOUT
    assert first.value.retryable is True

    unreachable, _ = _client(error=urllib.error.URLError("refused"))
    with pytest.raises(LLMRequestError) as second:
        unreachable.complete(({"role": "user", "content": "hi"},))
    assert second.value.category is LLMErrorCategory.CONNECTION
    assert second.value.retryable is True


def test_a_failure_message_does_not_echo_the_configured_endpoint() -> None:
    config = _config(base_url="http://secret-host.internal:11434")
    client = OllamaClient(config, opener=_FakeOpener(error=urllib.error.URLError("refused")))

    with pytest.raises(LLMRequestError) as error:
        client.complete(({"role": "user", "content": "hi"},))

    assert "secret-host.internal" not in str(error.value)


# ---------- provider independence ----------


def test_the_loop_produces_the_same_run_through_either_adapter(tmp_path: Any) -> None:
    """Stage J's exit criterion, asserted rather than asserted about.

    The same trajectory is driven twice: once by a scripted client returning the
    normalized objects directly, once by the local adapter converting provider-shaped
    JSON into them. The loop must not be able to tell the difference.
    """

    from datetime import UTC, datetime
    from pathlib import Path

    from proofcoder.agent import AgentLoop
    from proofcoder.events import MemorySink
    from proofcoder.llm.scripted import ScriptedClient
    from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
    from proofcoder.tools.files import create_list_files_tool
    from proofcoder.tools.finish import create_finish_task_tool
    from proofcoder.tools.registry import ToolRegistry

    run_id = "c" * 32
    moment = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)
    finish_arguments = {"summary": "Listed the workspace."}

    def registry(root: Path) -> ToolRegistry:
        built = ToolRegistry()
        built.register(create_list_files_tool(root))
        built.register(create_finish_task_tool(root))
        return built

    def run(client: object, root: Path) -> tuple[MemorySink, object]:
        sink = MemorySink()
        result = AgentLoop(
            client=client,  # type: ignore[arg-type]
            registry=registry(root),
            workspace=root,
            system_prompt="same prompt",
            max_steps=4,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
            random_value=lambda: 0.0,
            event_sink=sink,
            run_id_factory=lambda: run_id,
            event_clock=lambda: moment,
        ).run("list the workspace")
        return sink, result

    scripted_root = Path(tmp_path) / "scripted"
    local_root = Path(tmp_path) / "local"
    for root in (scripted_root, local_root):
        root.mkdir()
        (root / "visible.py").write_text("value = 1\n", encoding="utf-8")

    scripted = ScriptedClient(
        [
            ModelResponse(
                content=None,
                reasoning_content=None,
                finish_reason="tool_calls",
                usage=None,
                tool_calls=(
                    ToolCall(
                        id="ollama-tool-0",
                        function=FunctionCall(name="list_files", arguments="{}"),
                    ),
                ),
            ),
            ModelResponse(
                content=None,
                reasoning_content=None,
                finish_reason="tool_calls",
                usage=None,
                tool_calls=(
                    ToolCall(
                        id="ollama-tool-0",
                        function=FunctionCall(
                            name="finish_task",
                            arguments=json.dumps(finish_arguments, sort_keys=True),
                        ),
                    ),
                ),
            ),
        ]
    )

    class _SequenceOpener(_FakeOpener):
        def __init__(self, bodies: list[object]) -> None:
            super().__init__(None)
            self._bodies = list(bodies)

        def open(self, request: Any, timeout: float | None = None) -> Any:
            super_requests = self.requests
            super_requests.append({"url": request.full_url, "method": request.method})
            return _FakeResponse(json.dumps(self._bodies.pop(0)).encode("utf-8"))

    local = OllamaClient(
        _config(),
        opener=_SequenceOpener(
            [
                _assistant(
                    content=None,
                    tool_calls=[{"function": {"name": "list_files", "arguments": {}}}],
                ),
                _assistant(
                    content=None,
                    tool_calls=[
                        {"function": {"name": "finish_task", "arguments": finish_arguments}}
                    ],
                ),
            ]
        ),
    )

    scripted_sink, scripted_result = run(scripted, scripted_root)
    local_sink, local_result = run(local, local_root)

    def shape(sink: MemorySink) -> list[tuple[str, int]]:
        return [(event.event_type.value, event.step) for event in sink.events]

    assert shape(scripted_sink) == shape(local_sink)
    assert scripted_result.termination_reason == local_result.termination_reason  # type: ignore[union-attr]
    assert scripted_result.completion_status == local_result.completion_status  # type: ignore[union-attr]
    assert scripted_result.tool_call_count == local_result.tool_call_count  # type: ignore[union-attr]
