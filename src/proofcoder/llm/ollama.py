"""Local-model client for Ollama's native chat API.

This adapter exists to test the seam, not to add a vendor. Ollama's protocol differs
from Chat Completions in three ways that matter, and each one is a conversion this
module owns rather than something the loop is allowed to see:

* tool-call arguments arrive as a JSON **object**, while the rest of ProofCoder carries
  them as the untouched JSON **string** the model produced;
* tool calls carry no identifier, so one is synthesized here and matched back to the
  tool result by position, because the loop's whole protocol is built on those ids;
* tool results are addressed by name rather than by id.

It speaks HTTP through the standard library: proving an abstraction works is not a good
enough reason to add a runtime dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from http.client import HTTPException
from typing import Any

from proofcoder.config import ProofCoderConfig
from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.llm.base import ChatMessagePayload, ToolSchema
from proofcoder.protocol import FunctionCall, ModelResponse, TokenUsage, ToolCall

# Ollama has no documented ceiling of its own, so this is the same project choice the
# DeepSeek client makes: large enough for a whole-file write, small enough that one
# runaway generation cannot consume the whole time budget.
MAX_TOOL_OUTPUT_TOKENS = 8192
MAX_CONNECTIVITY_OUTPUT_TOKENS = 16
REQUEST_TIMEOUT_SECONDS = 300.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_CHAT_PATH = "/api/chat"
_TOOL_CALL_ID_PREFIX = "ollama-tool-"


class OllamaClient:
    """Send one synchronous, non-streaming request to a local Ollama server."""

    def __init__(
        self,
        config: ProofCoderConfig,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._opener = opener or urllib.request.build_opener()
        self._timeout_seconds = timeout_seconds

    def complete(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
    ) -> ModelResponse:
        """Make one chat request and normalize its response."""

        request: dict[str, object] = {
            "model": self._config.model,
            "messages": [_to_provider_message(message) for message in messages],
            "stream": False,
            "options": {
                "num_predict": (MAX_TOOL_OUTPUT_TOKENS if tools else MAX_CONNECTIVITY_OUTPUT_TOKENS)
            },
        }
        if tools:
            request["tools"] = list(tools)
        return _normalize_response(self._post(request))

    def check_connection(self) -> ModelResponse:
        """Make one minimal request so `doctor` can report reachability."""

        return self.complete(({"role": "user", "content": "ping"},))

    def _post(self, payload: Mapping[str, object]) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._config.base_url}{_CHAT_PATH}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise _status_error(error.code) from None
        except TimeoutError:
            raise LLMRequestError(
                "the local model did not respond before the request timeout",
                category=LLMErrorCategory.TIMEOUT,
            ) from None
        except (urllib.error.URLError, HTTPException, OSError):
            # The message deliberately says nothing about the address: it is operator
            # configuration, and a failure report is not the place to echo it back.
            raise LLMRequestError(
                "the local model endpoint could not be reached",
                category=LLMErrorCategory.CONNECTION,
            ) from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise LLMRequestError(
                f"the local model response exceeded {MAX_RESPONSE_BYTES} bytes",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LLMRequestError(
                "the local model response was not valid UTF-8 JSON",
                category=LLMErrorCategory.INVALID_RESPONSE,
            ) from None
        if not isinstance(document, dict):
            raise LLMRequestError(
                "the local model response was not a JSON object",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        return document


def _status_error(status: int) -> LLMRequestError:
    """Map one HTTP status onto the categories the retry policy already knows."""

    if status == 404:
        # Ollama answers 404 when the model is not pulled, which is a configuration
        # problem no retry can fix.
        return LLMRequestError(
            "the local model is not available on this server; pull it first",
            category=LLMErrorCategory.BAD_REQUEST,
            status_code=status,
        )
    if status == 429:
        return LLMRequestError(
            "the local model server is rate limiting requests",
            category=LLMErrorCategory.RATE_LIMIT,
            status_code=status,
        )
    if status in {500, 503}:
        return LLMRequestError(
            "the local model server reported a transient failure",
            category=LLMErrorCategory.SERVER,
            status_code=status,
        )
    if 400 <= status < 500:
        return LLMRequestError(
            "the local model server rejected the request",
            category=LLMErrorCategory.BAD_REQUEST,
            status_code=status,
        )
    return LLMRequestError(
        "the local model server returned an unexpected status",
        category=LLMErrorCategory.PERMANENT,
        status_code=status,
    )


def _to_provider_message(message: ChatMessagePayload) -> dict[str, object]:
    """Convert one ProofCoder message into Ollama's shape."""

    role = message.get("role")
    if role == "tool":
        # Ollama addresses a tool result by name rather than by id. The id stays out of
        # the request entirely; it is ProofCoder's bookkeeping, not the provider's.
        return {"role": "tool", "content": _text(message.get("content"))}
    if role == "assistant":
        assistant: dict[str, object] = {
            "role": "assistant",
            "content": _text(message.get("content")),
        }
        calls = message.get("tool_calls")
        if isinstance(calls, list) and calls:
            assistant["tool_calls"] = [_to_provider_tool_call(call) for call in calls]
        return assistant
    return {"role": str(role), "content": _text(message.get("content"))}


def _to_provider_tool_call(call: object) -> dict[str, object]:
    if not isinstance(call, Mapping):
        return {"function": {"name": "", "arguments": {}}}
    function = call.get("function")
    name = ""
    arguments: object = {}
    if isinstance(function, Mapping):
        name = _text(function.get("name"))
        raw = function.get("arguments")
        if isinstance(raw, str):
            try:
                arguments = json.loads(raw)
            except json.JSONDecodeError:
                # Sending the string back as-is would be a silent protocol change; an
                # empty object keeps the shape Ollama expects and stays honest about
                # having nothing usable to send.
                arguments = {}
        elif isinstance(raw, Mapping):
            arguments = dict(raw)
    return {"function": {"name": name, "arguments": arguments}}


def _normalize_response(document: Mapping[str, Any]) -> ModelResponse:
    """Convert one Ollama response into the object the rest of ProofCoder uses."""

    message = document.get("message")
    if not isinstance(message, Mapping):
        raise LLMRequestError(
            "the local model response contained no assistant message",
            category=LLMErrorCategory.INVALID_RESPONSE,
        )
    content = message.get("content")
    thinking = message.get("thinking")
    tool_calls = _normalize_tool_calls(message.get("tool_calls"))
    return ModelResponse(
        content=content if isinstance(content, str) and content else None,
        reasoning_content=thinking if isinstance(thinking, str) and thinking else None,
        finish_reason="tool_calls" if tool_calls else _finish_reason(document),
        usage=_normalize_usage(document),
        tool_calls=tool_calls,
    )


def _finish_reason(document: Mapping[str, Any]) -> str | None:
    reason = document.get("done_reason")
    if isinstance(reason, str) and reason:
        # Ollama says "stop" and "length" where Chat Completions says the same, so the
        # value passes through; anything else is reported verbatim rather than guessed at.
        return reason
    return "stop" if document.get("done") is True else None


def _normalize_tool_calls(value: object) -> tuple[ToolCall, ...]:
    if not isinstance(value, list) or not value:
        return ()
    calls: list[ToolCall] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise LLMRequestError(
                "the local model returned a malformed tool call",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        function = item.get("function")
        if not isinstance(function, Mapping):
            raise LLMRequestError(
                "the local model returned a tool call without a function",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise LLMRequestError(
                "the local model returned a tool call without a name",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        calls.append(
            ToolCall(
                # Ollama issues no identifier, so one is synthesized. It is stable
                # within a response and never leaves ProofCoder, which is all the
                # loop's call/result pairing needs.
                id=f"{_TOOL_CALL_ID_PREFIX}{index}",
                function=FunctionCall(
                    name=name,
                    arguments=_arguments_to_json(function.get("arguments")),
                ),
            )
        )
    return tuple(calls)


def _arguments_to_json(value: object) -> str:
    """Render provider-side arguments as the JSON string the rest of the code expects."""

    if isinstance(value, str):
        return value
    if value is None:
        return "{}"
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raise LLMRequestError(
            "the local model returned tool arguments that are not representable as JSON",
            category=LLMErrorCategory.INVALID_RESPONSE,
        ) from None


def _normalize_usage(document: Mapping[str, Any]) -> TokenUsage | None:
    prompt = document.get("prompt_eval_count")
    completion = document.get("eval_count")
    if not isinstance(prompt, int) and not isinstance(completion, int):
        return None
    prompt_tokens = prompt if isinstance(prompt, int) else None
    completion_tokens = completion if isinstance(completion, int) else None
    total = (
        None
        if prompt_tokens is None or completion_tokens is None
        else prompt_tokens + completion_tokens
    )
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total,
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
