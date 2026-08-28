"""Offline integration tests for Stage D2 recovery and termination boundaries."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from proofcoder.agent import NO_PROGRESS_MESSAGE, PROTOCOL_REPAIR_MESSAGE, AgentLoop
from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.llm.base import ChatMessagePayload, ToolSchema
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import (
    AssistantMessage,
    FunctionCall,
    ModelResponse,
    TerminationReason,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import create_create_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry


def _call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=arguments))


def _response(
    *,
    content: str | None = None,
    calls: tuple[ToolCall, ...] = (),
) -> ModelResponse:
    return AssistantMessage(
        content=content,
        reasoning_content="private",
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _finish(call_id: str = "finish") -> ToolCall:
    return _call(call_id, "finish_task", '{"summary":"done","changed_files":[]}')


def _registry(workspace: Path, *, probe: bool = False, create: bool = False) -> ToolRegistry:
    registry = ToolRegistry()
    if probe:
        registry.register(
            ToolDefinition(
                name="probe",
                description="Return one stable local observation.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                execute=lambda arguments: ToolResult.success({"value": 1}),
            )
        )
    if create:
        registry.register(create_create_file_tool(workspace))
    registry.register(create_finish_task_tool(workspace))
    return registry


def _loop(
    workspace: Path,
    client: object,
    *,
    registry: ToolRegistry | None = None,
    max_steps: int = 12,
    **kwargs: object,
) -> AgentLoop:
    return AgentLoop(
        client=client,  # type: ignore[arg-type]
        registry=_registry(workspace) if registry is None else registry,
        workspace=workspace,
        system_prompt="system",
        max_steps=max_steps,
        **kwargs,  # type: ignore[arg-type]
    )


def _tool_payloads(result: object) -> list[dict[str, object]]:
    history = result.history  # type: ignore[attr-defined]
    return [
        json.loads(message.content)
        for message in history.messages
        if isinstance(message, ToolMessage)
    ]


def test_transient_api_failures_retry_locally_with_identical_requests(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            LLMRequestError("safe", category=LLMErrorCategory.CONNECTION),
            LLMRequestError(
                "safe",
                category=LLMErrorCategory.SERVER,
                status_code=503,
            ),
            _response(calls=(_finish(),)),
        ]
    )
    delays: list[float] = []

    result = _loop(
        tmp_path,
        client,
        sleep=delays.append,
        random_value=lambda: 0.0,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.api_attempt_count == 3
    assert result.api_retry_count == 2
    assert result.model_call_count == 1
    assert delays == [0.5, 1.0]
    assert client.requests[0] == client.requests[1] == client.requests[2]
    assert result.warnings == ("API_RETRY", "API_RETRY")


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (LLMErrorCategory.CONNECTION, None),
        (LLMErrorCategory.TIMEOUT, None),
        (LLMErrorCategory.RATE_LIMIT, 429),
        (LLMErrorCategory.SERVER, 500),
        (LLMErrorCategory.SERVER, 503),
    ],
)
def test_each_allowed_transient_error_can_recover(
    tmp_path: Path,
    category: LLMErrorCategory,
    status: int | None,
) -> None:
    client = ScriptedClient(
        [
            LLMRequestError("safe", category=category, status_code=status),
            _response(calls=(_finish(),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        sleep=lambda seconds: None,
        random_value=lambda: 0.0,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.api_attempt_count == 2
    assert result.api_retry_count == 1
    assert result.model_call_count == 1


@pytest.mark.parametrize(
    ("category", "status"),
    [
        (LLMErrorCategory.BAD_REQUEST, 400),
        (LLMErrorCategory.AUTHENTICATION, 401),
        (LLMErrorCategory.PAYMENT_REQUIRED, 402),
        (LLMErrorCategory.UNPROCESSABLE, 422),
        (LLMErrorCategory.SERVER, 502),
        (LLMErrorCategory.PERMANENT, None),
    ],
)
def test_permanent_api_failure_is_not_retried(
    tmp_path: Path,
    category: LLMErrorCategory,
    status: int | None,
) -> None:
    client = ScriptedClient([LLMRequestError("safe", category=category, status_code=status)])

    result = _loop(tmp_path, client, clock=lambda: 0.0).run("task")

    assert result.termination_reason is TerminationReason.API_ERROR
    assert result.api_attempt_count == 1
    assert result.api_retry_count == 0
    assert result.model_call_count == 0


def test_retry_exhaustion_is_api_error_without_counting_a_model_step(tmp_path: Path) -> None:
    errors = [LLMRequestError("safe", category=LLMErrorCategory.TIMEOUT) for _ in range(3)]
    result = _loop(
        tmp_path,
        ScriptedClient(errors),
        sleep=lambda seconds: None,
        random_value=lambda: 0.0,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.API_ERROR
    assert result.api_attempt_count == 3
    assert result.api_retry_count == 2
    assert result.model_call_count == 0


def test_retry_after_that_cannot_fit_remaining_time_stops_at_time_limit(
    tmp_path: Path,
) -> None:
    error = LLMRequestError(
        "safe",
        category=LLMErrorCategory.RATE_LIMIT,
        status_code=429,
        retry_after_seconds=30,
    )
    delays: list[float] = []

    result = _loop(
        tmp_path,
        ScriptedClient([error]),
        max_seconds=10,
        sleep=delays.append,
        random_value=lambda: 0.0,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.MAX_TIME
    assert result.api_attempt_count == 1
    assert result.api_retry_count == 0
    assert delays == []


def test_first_missing_tool_response_gets_one_repair_then_tools_continue(
    tmp_path: Path,
) -> None:
    client = ScriptedClient([_response(content="text only"), _response(calls=(_finish(),))])

    result = _loop(tmp_path, client, clock=lambda: 0.0).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.model_call_count == 2
    assert result.warnings == ("PROTOCOL_REPAIR",)
    assert client.requests[1].messages[-2]["role"] == "assistant"
    assert client.requests[1].messages[-1] == {
        "role": "user",
        "content": PROTOCOL_REPAIR_MESSAGE,
    }
    assert any(
        isinstance(message, UserMessage) and message.content == PROTOCOL_REPAIR_MESSAGE
        for message in result.history.messages
    )


def test_second_missing_tool_response_stops_model(tmp_path: Path) -> None:
    result = _loop(
        tmp_path,
        ScriptedClient([_response(content="one"), _response(content="two")]),
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.model_call_count == 2
    assert result.completion_status is None


def test_consecutive_invalid_batches_stop_without_api_retries(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("bad-1", "missing", "{"),)),
            _response(calls=(_call("bad-2", "missing", "{"),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        max_consecutive_failures=2,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.MAX_CONSECUTIVE_FAILURES
    assert result.consecutive_failure_count == 2
    assert result.api_attempt_count == 2
    assert result.api_retry_count == 0


def test_ordinary_success_resets_consecutive_failure_count(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("bad-1", "missing"),)),
            _response(calls=(_call("ok", "probe"),)),
            _response(calls=(_call("bad-2", "missing"),)),
            _response(calls=(_finish(),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=_registry(tmp_path, probe=True),
        max_consecutive_failures=2,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.consecutive_failure_count == 1


def test_three_identical_successful_batches_stop_for_no_progress(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("probe-1", "probe"),)),
            _response(calls=(_call("probe-2", "probe"),)),
            _response(calls=(_call("probe-3", "probe"),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=_registry(tmp_path, probe=True),
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.NO_PROGRESS
    assert result.no_progress_count == 3
    assert result.warnings == ("NO_PROGRESS",)
    assert client.requests[2].messages[-1] == {
        "role": "user",
        "content": NO_PROGRESS_MESSAGE,
    }


def test_three_identical_failed_batches_also_stop_for_no_progress(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("missing-1", "missing"),)),
            _response(calls=(_call("missing-2", "missing"),)),
            _response(calls=(_call("missing-3", "missing"),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        max_consecutive_failures=5,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.NO_PROGRESS
    assert result.consecutive_failure_count == 3
    assert result.no_progress_count == 3


def test_successful_modification_resets_no_progress_sequence(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("probe-1", "probe"),)),
            _response(calls=(_call("probe-2", "probe"),)),
            _response(
                calls=(
                    _call(
                        "create",
                        "create_file",
                        '{"path":"created.txt","content":"value"}',
                    ),
                )
            ),
            _response(calls=(_call("probe-3", "probe"),)),
            _response(calls=(_call("probe-4", "probe"),)),
            _response(
                calls=(
                    _call(
                        "finish",
                        "finish_task",
                        '{"summary":"changed","changed_files":["created.txt"]}',
                    ),
                )
            ),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=_registry(tmp_path, probe=True, create=True),
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.no_progress_count == 2
    assert result.warnings == ("NO_PROGRESS", "NO_PROGRESS")


@dataclass
class _FakeClock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


def test_time_limit_after_started_batch_keeps_every_tool_result(tmp_path: Path) -> None:
    clock = _FakeClock()
    registry = ToolRegistry()

    def advance(arguments: Mapping[str, object]) -> ToolResult:
        clock.value = 11
        return ToolResult.success({"done": True})

    registry.register(
        ToolDefinition(
            name="slow",
            description="Advance fake time.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=advance,
        )
    )
    result = _loop(
        tmp_path,
        ScriptedClient([_response(calls=(_call("slow", "slow"),))]),
        registry=registry,
        max_seconds=10,
        clock=clock,
    ).run("task")

    assert result.termination_reason is TerminationReason.MAX_TIME
    assert len(_tool_payloads(result)) == 1
    assert _tool_payloads(result)[0]["ok"] is True


def test_time_limit_before_batch_marks_every_call_unexecuted(tmp_path: Path) -> None:
    clock = _FakeClock()
    executed = False

    class AdvancingClient:
        def complete(
            self,
            messages: Sequence[ChatMessagePayload],
            tools: Sequence[ToolSchema] = (),
        ) -> ModelResponse:
            clock.value = 11
            return _response(calls=(_call("one", "would_run"),))

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        nonlocal executed
        executed = True
        return ToolResult.success({})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="would_run",
            description="Must not execute.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=execute,
        )
    )
    result = _loop(
        tmp_path,
        AdvancingClient(),
        registry=registry,
        max_seconds=10,
        clock=clock,
    ).run("task")

    assert result.termination_reason is TerminationReason.MAX_TIME
    assert executed is False
    assert _tool_payloads(result)[0]["error"]["code"] == "BATCH_NOT_STARTED"
    assert _tool_payloads(result)[0]["data"]["execution_started"] is False


def test_keyboard_interrupt_in_batch_completes_all_tool_ids(tmp_path: Path) -> None:
    executed: list[str] = []
    registry = ToolRegistry()

    def first(arguments: Mapping[str, object]) -> ToolResult:
        executed.append("first")
        return ToolResult.success({"done": "first"})

    def interrupt(arguments: Mapping[str, object]) -> ToolResult:
        executed.append("second")
        raise KeyboardInterrupt

    def third(arguments: Mapping[str, object]) -> ToolResult:
        executed.append("third")
        return ToolResult.success({"done": "third"})

    for name, executor in (("first", first), ("second", interrupt), ("third", third)):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                execute=executor,
            )
        )
    calls = tuple(_call(name, name) for name in ("first", "second", "third"))

    result = _loop(
        tmp_path,
        ScriptedClient([_response(calls=calls)]),
        registry=registry,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert executed == ["first", "second"]
    payloads = _tool_payloads(result)
    assert [payload["error"]["code"] if payload["error"] else None for payload in payloads] == [
        None,
        "TOOL_INTERRUPTED",
        "BATCH_INTERRUPTED",
    ]
    assert payloads[1]["data"]["execution_started"] is True
    assert payloads[2]["data"]["execution_started"] is False


def test_keyboard_interrupt_preserves_prior_modification_as_unverified(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(create_create_file_tool(tmp_path))
    registry.register(
        ToolDefinition(
            name="interrupt",
            description="Interrupt the offline test run.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=lambda arguments: _raise_keyboard_interrupt(),
        )
    )
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call(
                        "create",
                        "create_file",
                        '{"path":"created.txt","content":"value"}',
                    ),
                )
            ),
            _response(calls=(_call("interrupt", "interrupt"),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=registry,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert result.completion_status is None
    assert result.changed_files == ("created.txt",)
    assert result.verification_command is None
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "value"
    assert [
        message.tool_call_id
        for message in result.history.messages
        if isinstance(message, ToolMessage)
    ] == [
        "create",
        "interrupt",
    ]


def test_keyboard_interrupt_during_model_request_is_controlled(tmp_path: Path) -> None:
    class InterruptingClient:
        def complete(
            self,
            messages: Sequence[ChatMessagePayload],
            tools: Sequence[ToolSchema] = (),
        ) -> ModelResponse:
            raise KeyboardInterrupt

    result = _loop(tmp_path, InterruptingClient(), clock=lambda: 0.0).run("task")

    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert result.model_call_count == 0
    assert result.api_attempt_count == 1


def test_system_exit_is_not_converted_to_interrupted(tmp_path: Path) -> None:
    class ExitingClient:
        def complete(
            self,
            messages: Sequence[ChatMessagePayload],
            tools: Sequence[ToolSchema] = (),
        ) -> ModelResponse:
            raise SystemExit(7)

    with pytest.raises(SystemExit, match="7"):
        _loop(tmp_path, ExitingClient(), clock=lambda: 0.0).run("task")


def test_unexpected_client_exception_is_internal_error(tmp_path: Path) -> None:
    class BrokenClient:
        def complete(
            self,
            messages: Sequence[ChatMessagePayload],
            tools: Sequence[ToolSchema] = (),
        ) -> ModelResponse:
            raise RuntimeError("private implementation detail")

    result = _loop(tmp_path, BrokenClient(), clock=lambda: 0.0).run("task")

    assert result.termination_reason is TerminationReason.INTERNAL_ERROR
    assert result.completion_status is None


def test_required_context_over_budget_stops_without_api_request(tmp_path: Path) -> None:
    client = ScriptedClient([_response(calls=(_finish(),))])

    result = _loop(
        tmp_path,
        client,
        context_budget_bytes=1,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.CONTEXT_BUDGET_EXCEEDED
    assert result.api_attempt_count == 0
    assert result.model_call_count == 0
    assert client.requests == []


def test_context_compaction_warns_and_retains_latest_complete_group(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="large_probe",
            description="Return a stable bounded observation.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=lambda arguments: ToolResult.success({"text": "x" * 2500}),
        )
    )
    registry.register(create_finish_task_tool(tmp_path))
    client = ScriptedClient(
        [
            _response(calls=(_call("probe-1", "large_probe"),)),
            _response(calls=(_call("probe-2", "large_probe"),)),
            _response(calls=(_finish(),)),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=registry,
        context_budget_bytes=6000,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.context_compaction_count == 1
    assert "CONTEXT_COMPACTED" in result.warnings
    final_messages = client.requests[-1].messages
    assert all(message.get("tool_call_id") != "probe-1" for message in final_messages)
    assert any(message.get("tool_call_id") == "probe-2" for message in final_messages)


def test_compaction_then_modify_verify_finish_remains_locally_verified(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="large_probe",
            description="Return a bounded observation selected by marker.",
            parameters={
                "type": "object",
                "properties": {"marker": {"type": "string"}},
                "required": ["marker"],
                "additionalProperties": False,
            },
            execute=lambda arguments: ToolResult.success(
                {"marker": arguments["marker"], "text": "x" * 2500}
            ),
        )
    )
    registry.register(create_create_file_tool(tmp_path))
    registry.register(
        create_run_command_tool(
            tmp_path,
            environ={
                "PATH": str(Path(sys.executable).resolve().parent),
                "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
                "TEMP": str(tmp_path),
                "TMP": str(tmp_path),
                "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            },
        )
    )
    registry.register(create_finish_task_tool(tmp_path))
    verification = ["python", "-m", "compileall", "-q", "created.py"]
    client = ScriptedClient(
        [
            _response(calls=(_call("probe-1", "large_probe", '{"marker":"one"}'),)),
            _response(calls=(_call("probe-2", "large_probe", '{"marker":"two"}'),)),
            _response(
                calls=(
                    _call(
                        "create",
                        "create_file",
                        '{"path":"created.py","content":"VALUE = 1\\n"}',
                    ),
                )
            ),
            _response(
                calls=(
                    _call(
                        "verify",
                        "run_command",
                        json.dumps({"argv": verification, "timeout_seconds": 10}),
                    ),
                )
            ),
            _response(
                calls=(
                    _call(
                        "finish",
                        "finish_task",
                        json.dumps(
                            {
                                "summary": "created and compiled",
                                "changed_files": ["created.py"],
                                "verification_command": verification,
                            }
                        ),
                    ),
                )
            ),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=registry,
        context_budget_bytes=9000,
        clock=lambda: 0.0,
    ).run("task")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.completion_status is not None
    assert result.completion_status.value == "completed_verified"
    assert result.context_compaction_count >= 1
    assert result.changed_files == ("created.py",)
    assert result.verification_command == tuple(verification)


def test_time_limit_before_first_model_request_makes_no_api_attempt(tmp_path: Path) -> None:
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 11.0

    client = ScriptedClient([_response(calls=(_finish(),))])
    result = _loop(tmp_path, client, max_seconds=10, clock=clock).run("task")

    assert result.termination_reason is TerminationReason.MAX_TIME
    assert result.api_attempt_count == 0
    assert client.requests == []


def _raise_keyboard_interrupt() -> ToolResult:
    raise KeyboardInterrupt
