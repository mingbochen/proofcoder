"""Offline tests for streamed-response assembly.

Assembly is where a stream stops being a transport detail, so every rule that keeps a
streamed response indistinguishable from a non-streamed one is checked here, together
with every way assembly can fail without executing anything.
"""

from __future__ import annotations

import json

import pytest

from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.llm.streaming import (
    MAX_STREAM_TOOL_CALLS,
    MAX_TOOL_ARGUMENT_CHARACTERS,
    StreamAssembler,
    truncated_stream_error,
)
from proofcoder.protocol import TokenUsage


def test_text_is_reported_incrementally_and_joined_once() -> None:
    seen: list[str] = []
    assembler = StreamAssembler(on_text=seen.append)

    assembler.add_text("Look")
    assembler.add_text("ing at ")
    assembler.add_text("the file.")
    assembler.set_finish_reason("stop")
    response = assembler.finish()

    assert seen == ["Look", "ing at ", "the file."]
    assert response.content == "Looking at the file."
    assert response.finish_reason == "stop"
    assert response.tool_calls == ()


def test_tool_call_fragments_never_reach_the_incremental_callback() -> None:
    """Half a tool call reads as an operation that has already started. None has."""

    seen: list[str] = []
    assembler = StreamAssembler(on_text=seen.append)

    assembler.add_tool_call_fragment(0, call_id="c1", name="read_file")
    assembler.add_tool_call_fragment(0, arguments='{"path"')
    assembler.add_tool_call_fragment(0, arguments=': "a.py"}')
    assembler.finish()

    assert seen == []


def test_reasoning_is_kept_but_never_reported_incrementally() -> None:
    seen: list[str] = []
    assembler = StreamAssembler(on_text=seen.append)

    assembler.add_reasoning("private thought")
    assembler.add_text("visible")
    response = assembler.finish()

    assert seen == ["visible"]
    assert response.reasoning_content == "private thought"


def test_fragments_are_merged_by_index_in_first_seen_order() -> None:
    assembler = StreamAssembler()

    assembler.add_tool_call_fragment(1, call_id="second", name="list_files")
    assembler.add_tool_call_fragment(0, call_id="first", name="read_file")
    assembler.add_tool_call_fragment(0, arguments='{"path": ')
    assembler.add_tool_call_fragment(1, arguments="{}")
    assembler.add_tool_call_fragment(0, arguments='"a.py"}')
    response = assembler.finish()

    assert [call.id for call in response.tool_calls] == ["second", "first"]
    assert response.tool_calls[1].function.arguments == '{"path": "a.py"}'
    assert json.loads(response.tool_calls[1].function.arguments) == {"path": "a.py"}
    assert response.finish_reason == "tool_calls"


def test_a_call_without_an_identifier_still_gets_one() -> None:
    assembler = StreamAssembler()

    assembler.add_tool_call_fragment(0, name="list_files", arguments="{}")
    response = assembler.finish()

    assert response.tool_calls[0].id
    assert response.tool_calls[0].function.name == "list_files"


def test_missing_arguments_assemble_into_an_empty_object() -> None:
    assembler = StreamAssembler()

    assembler.add_tool_call_fragment(0, call_id="c", name="list_files")
    response = assembler.finish()

    assert response.tool_calls[0].function.arguments == "{}"


def test_usage_is_carried_through() -> None:
    assembler = StreamAssembler()

    assembler.add_text("hi")
    assembler.set_usage(TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7))
    response = assembler.finish()

    assert response.usage is not None
    assert response.usage.total_tokens == 7


# ---------- failures, none of which may produce a tool call ----------


def test_an_empty_stream_is_an_invalid_response() -> None:
    with pytest.raises(LLMRequestError) as error:
        StreamAssembler().finish()

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE
    assert error.value.retryable is False


def test_arguments_that_never_become_valid_json_are_refused() -> None:
    assembler = StreamAssembler()
    assembler.add_tool_call_fragment(0, call_id="c", name="read_file", arguments='{"path"')

    with pytest.raises(LLMRequestError) as error:
        assembler.finish()

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE


def test_arguments_that_assemble_into_a_non_object_are_refused() -> None:
    assembler = StreamAssembler()
    assembler.add_tool_call_fragment(0, call_id="c", name="read_file", arguments="[1, 2]")

    with pytest.raises(LLMRequestError) as error:
        assembler.finish()

    assert error.value.category is LLMErrorCategory.INVALID_RESPONSE


def test_a_changed_identifier_is_a_protocol_violation() -> None:
    assembler = StreamAssembler()
    assembler.add_tool_call_fragment(0, call_id="c1", name="read_file")

    with pytest.raises(LLMRequestError) as error:
        assembler.add_tool_call_fragment(0, call_id="c2")

    assert error.value.category is LLMErrorCategory.PERMANENT
    assert error.value.retryable is False


def test_a_changed_function_name_is_a_protocol_violation() -> None:
    assembler = StreamAssembler()
    assembler.add_tool_call_fragment(0, call_id="c1", name="read_file")

    with pytest.raises(LLMRequestError) as error:
        assembler.add_tool_call_fragment(0, name="delete_path")

    assert error.value.category is LLMErrorCategory.PERMANENT


def test_a_call_that_never_names_a_function_is_refused() -> None:
    assembler = StreamAssembler()
    assembler.add_tool_call_fragment(0, call_id="c1", arguments="{}")

    with pytest.raises(LLMRequestError) as error:
        assembler.finish()

    assert error.value.category is LLMErrorCategory.PERMANENT


def test_too_many_tool_calls_are_refused() -> None:
    assembler = StreamAssembler()
    for index in range(MAX_STREAM_TOOL_CALLS):
        assembler.add_tool_call_fragment(index, call_id=f"c{index}", name="list_files")

    with pytest.raises(LLMRequestError) as error:
        assembler.add_tool_call_fragment(MAX_STREAM_TOOL_CALLS, call_id="x", name="list_files")

    assert error.value.category is LLMErrorCategory.PERMANENT


def test_oversized_arguments_are_refused() -> None:
    assembler = StreamAssembler()
    assembler.add_tool_call_fragment(0, call_id="c", name="create_file")

    with pytest.raises(LLMRequestError) as error:
        assembler.add_tool_call_fragment(0, arguments="x" * (MAX_TOOL_ARGUMENT_CHARACTERS + 1))

    assert error.value.category is LLMErrorCategory.PERMANENT


def test_a_truncated_stream_is_transient() -> None:
    error = truncated_stream_error()

    assert error.category is LLMErrorCategory.CONNECTION
    assert error.retryable is True


def test_a_refused_assembly_yields_no_response_at_all() -> None:
    """The point of every failure above: nothing partial escapes to the loop."""

    assembler = StreamAssembler()
    assembler.add_text("I will read the file now.")
    assembler.add_tool_call_fragment(0, call_id="c", name="read_file", arguments="{bad")

    with pytest.raises(LLMRequestError):
        assembler.finish()


# ---------- the seam: the loop never learns a stream happened ----------


def _streaming_response() -> object:
    from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall

    return ModelResponse(
        content="Listing now.",
        reasoning_content=None,
        finish_reason="tool_calls",
        usage=None,
        tool_calls=(ToolCall(id="c1", function=FunctionCall(name="list_files", arguments="{}")),),
    )


class _FakeStreamingClient:
    """Streams text in pieces; refuses to be used through the non-streaming path."""

    def __init__(self) -> None:
        self.streamed = False

    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        raise AssertionError("the caller asked for streaming but used complete()")

    def complete_streaming(self, messages, tools=(), *, on_text=None):  # type: ignore[no-untyped-def]
        self.streamed = True
        for piece in ("List", "ing now."):
            if on_text:
                on_text(piece)
        return _streaming_response()


class _PlainClient:
    def complete(self, messages, tools=()):  # type: ignore[no-untyped-def]
        return _streaming_response()


def test_the_wrapper_presents_a_stream_through_the_ordinary_seam() -> None:
    from proofcoder.llm.base import StreamingClient, supports_streaming

    inner = _FakeStreamingClient()
    seen: list[str] = []
    client = StreamingClient(inner=inner, on_text=seen.append)  # type: ignore[arg-type]

    response = client.complete(({"role": "user", "content": "hi"},))

    assert inner.streamed is True
    assert seen == ["List", "ing now."]
    # What the caller receives is the ordinary response object, unchanged.
    assert response.content == "Listing now."
    assert response.tool_calls[0].function.name == "list_files"
    assert supports_streaming(inner) is True
    assert supports_streaming(_PlainClient()) is False


def test_asking_for_streaming_from_a_provider_without_it_is_a_configuration_error() -> None:
    from proofcoder.errors import ConfigurationError
    from proofcoder.llm.factory import create_client

    class _Config:
        provider = "neither"

    with pytest.raises((ConfigurationError, AttributeError)):
        create_client(_Config(), stream=True)  # type: ignore[arg-type]


def test_the_browser_exposes_streamed_text_and_drops_it_when_the_event_lands(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The preview is a side channel, not an event, and it must not outlive the event."""

    from pathlib import Path

    from proofcoder.agent_runtime import AgentRunLimits
    from proofcoder.web.runs import BrowserRun, BrowserRunStatus

    run = BrowserRun(
        run_id="a" * 32,
        workspace=Path(tmp_path),
        task="look",
        limits=AgentRunLimits(),
        started_at="2026-09-17T00:00:00.000Z",
        stream=True,
    )

    assert run.summary().partial_text == ""
    run.append_partial_text("List")
    run.append_partial_text("ing now.")
    assert run.summary().partial_text == "Listing now."

    run.clear_partial_text()
    assert run.summary().partial_text == ""
    assert run.summary().status is BrowserRunStatus.RUNNING
