"""Offline tests for cooperative AgentLoop cancellation.

``KeyboardInterrupt`` only reaches the main thread, so a caller that drives the loop
from a worker thread needs an explicit stop signal. These tests pin the checkpoints
where that signal is observed and prove it reports the existing interrupted
termination rather than a new outcome.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from proofcoder.agent import AgentLoop
from proofcoder.agent_runtime import create_agent_runtime_resources
from proofcoder.events import MemorySink
from proofcoder.llm.base import ChatMessagePayload, ToolSchema
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.prompt import STAGE_B_SYSTEM_PROMPT
from proofcoder.protocol import (
    FunctionCall,
    ModelResponse,
    TerminationReason,
    ToolCall,
)
from proofcoder.tools.registry import ToolRegistry


class _CancellingClient:
    """Set the cancel flag after a chosen number of completed model calls."""

    def __init__(self, responses: Sequence[ModelResponse], cancel_after: int) -> None:
        self._scripted = ScriptedClient(list(responses))
        self._cancel_after = cancel_after
        self.call_count = 0
        self.cancelled = False

    def complete(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
    ) -> ModelResponse:
        response = self._scripted.complete(messages, tools)
        self.call_count += 1
        if self.call_count >= self._cancel_after:
            self.cancelled = True
        return response


def _response(*, content: str | None = None, calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content="private",
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _create_call(call_id: str, path: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(
            name="create_file",
            arguments=json.dumps({"path": path, "content": "x\n"}),
        ),
    )


def _loop(
    workspace: Path,
    client: object,
    *,
    sink: MemorySink,
    cancel_requested: object = None,
    max_steps: int = 4,
) -> AgentLoop:
    registry = create_agent_runtime_resources(workspace).registry
    assert isinstance(registry, ToolRegistry)
    return AgentLoop(
        client=client,  # type: ignore[arg-type]
        registry=registry,
        workspace=workspace,
        system_prompt=STAGE_B_SYSTEM_PROMPT,
        max_steps=max_steps,
        event_sink=sink,
        cancel_requested=cancel_requested,  # type: ignore[arg-type]
    )


def test_cancellation_between_steps_reports_interrupted(tmp_path: Path) -> None:
    client = _CancellingClient([_response(content="no tool calls")], cancel_after=1)
    sink = MemorySink()
    loop = _loop(tmp_path, client, sink=sink, cancel_requested=lambda: client.cancelled)

    result = loop.run("stop me between steps")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert result.completion_status is None
    assert client.call_count == 1


def test_cancellation_stops_a_batch_before_its_first_side_effect(tmp_path: Path) -> None:
    client = _CancellingClient(
        [_response(calls=(_create_call("create-1", "first.txt"),))],
        cancel_after=1,
    )
    sink = MemorySink()
    loop = _loop(tmp_path, client, sink=sink, cancel_requested=lambda: client.cancelled)

    result = loop.run("stop me before the write")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert not (tmp_path / "first.txt").exists()
    assert result.changed_files == ()


def test_cancellation_stops_a_batch_between_two_calls(tmp_path: Path) -> None:
    calls = (_create_call("create-1", "first.txt"), _create_call("create-2", "second.txt"))
    cancelled = False
    created: list[str] = []

    def cancel_requested() -> bool:
        # The first call executes, the flag is raised, and the second must not run.
        nonlocal cancelled
        if created:
            cancelled = True
        return cancelled

    client = ScriptedClient([_response(calls=calls)])
    sink = MemorySink()
    resources = create_agent_runtime_resources(tmp_path)
    loop = AgentLoop(
        client=client,
        registry=resources.registry,
        workspace=tmp_path,
        system_prompt=STAGE_B_SYSTEM_PROMPT,
        max_steps=2,
        event_sink=sink,
        cancel_requested=cancel_requested,
    )
    original_execute = resources.registry.execute

    def recording_execute(prepared: object) -> object:
        result = original_execute(prepared)  # type: ignore[arg-type]
        created.append("done")
        return result

    resources.registry.execute = recording_execute  # type: ignore[method-assign, assignment]

    result = loop.run("stop me mid batch")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_no_cancel_hook_leaves_termination_unchanged(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_create_call("create-1", "kept.txt"),)),
            _response(content="done"),
            _response(content="done"),
        ]
    )
    sink = MemorySink()
    loop = _loop(tmp_path, client, sink=sink, max_steps=3)

    result = loop.run("run without a cancel hook")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert (tmp_path / "kept.txt").exists()


def test_cancellation_after_a_model_response_skips_the_whole_batch(tmp_path: Path) -> None:
    client = _CancellingClient(
        [
            _response(
                calls=(
                    _create_call("create-1", "a.txt"),
                    _create_call("create-2", "b.txt"),
                )
            )
        ],
        cancel_after=1,
    )
    sink = MemorySink()
    loop = _loop(tmp_path, client, sink=sink, cancel_requested=lambda: client.cancelled)

    result = loop.run("cancel before tools start")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert result.tool_call_count == 2
    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert result.elapsed_seconds >= 0.0
