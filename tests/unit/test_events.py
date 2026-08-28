"""Structured event ordering, redaction, bounds, and sink tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from proofcoder.agent import AgentLoop
from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.events import (
    MAX_EVENT_JSON_BYTES,
    CompositeSink,
    EventEmitter,
    EventSinkError,
    EventType,
    MemorySink,
    NoOpSink,
    RunEvent,
    TerminalSink,
    diff_event_payload,
    sanitize_payload,
    summarize_tool_arguments,
    summarize_tool_result,
)
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, TokenUsage, ToolCall
from proofcoder.safety.secrets import redact_text
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry

RUN_ID = "a" * 32
SECRET = "obviously-fake-event-secret"
FIXED_TIME = datetime(2026, 8, 28, 1, 2, 3, 456789, tzinfo=UTC)


def _response(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        content=f"visible text {SECRET}",
        reasoning_content=f"private reasoning {SECRET}",
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        tool_calls=tuple(calls),
    )


def _call(call_id: str) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name="probe", arguments="{}"))


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="probe",
            description="Return a stable observation.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=lambda arguments: ToolResult.success({"value": 1}),
        )
    )
    return registry


def test_emitter_allocates_complete_strict_events_and_memory_sink() -> None:
    memory = MemorySink()
    emitter = EventEmitter(run_id=RUN_ID, sink=memory, clock=lambda: FIXED_TIME)

    first = emitter.emit(EventType.TASK, step=0, payload={"task": "task"})
    second = emitter.emit(EventType.WARNING, step=2, payload={"code": "SAFE"})

    assert memory.events == [first, second]
    assert [event.sequence for event in memory.events] == [1, 2]
    assert all(event.run_id == RUN_ID for event in memory.events)
    assert [event.step for event in memory.events] == [0, 2]
    assert all(event.timestamp == "2026-08-28T01:02:03.456789Z" for event in memory.events)
    assert all(event.schema_version == 1 for event in memory.events)


def test_noop_sink_accepts_events_without_storage() -> None:
    emitter = EventEmitter(run_id=RUN_ID, sink=NoOpSink(), clock=lambda: FIXED_TIME)

    emitter.emit(EventType.TASK, step=0, payload={"task": "task"})

    assert emitter.event_count == 1
    assert emitter.trace_complete is True


def test_agent_emits_multi_call_events_in_real_order_with_matching_ids(tmp_path: Path) -> None:
    memory = MemorySink()
    client = ScriptedClient([_response(_call("first"), _call("second"))])

    result = AgentLoop(
        client=client,
        registry=_registry(),
        workspace=tmp_path,
        system_prompt="system",
        max_steps=1,
        event_sink=memory,
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        sensitive_values=(SECRET,),
        clock=lambda: 0.0,
    ).run(f"task {SECRET}")

    assert [event.sequence for event in memory.events] == list(range(1, len(memory.events) + 1))
    assert [event.event_type for event in memory.events] == [
        EventType.TASK,
        EventType.MODEL,
        EventType.TOOL_CALL,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.TOOL_RESULT,
        EventType.TERMINATION,
    ]
    calls = [event for event in memory.events if event.event_type is EventType.TOOL_CALL]
    results = [event for event in memory.events if event.event_type is EventType.TOOL_RESULT]
    assert [event.payload["tool_call_id"] for event in calls] == ["first", "second"]
    assert [event.payload["tool_call_id"] for event in results] == ["first", "second"]
    assert result.input_token_count == 11
    assert result.output_token_count == 7
    assert result.final_report is not None
    assert "- input_tokens=11" in result.final_report
    assert "- output_tokens=7" in result.final_report
    termination = memory.events[-1]
    assert type(termination.payload["input_tokens"]) is int
    assert type(termination.payload["output_tokens"]) is int
    assert termination.payload["input_tokens"] == 11
    assert termination.payload["output_tokens"] == 7
    assert result.run_id == RUN_ID
    assert result.event_count == len(memory.events)
    serialized = "\n".join(event.to_json() for event in memory.events)
    assert SECRET not in serialized
    assert "private reasoning" not in serialized
    assert "reasoning_content" not in serialized


def test_first_result_is_emitted_before_second_tool_starts(tmp_path: Path) -> None:
    memory = MemorySink()
    registry = ToolRegistry()

    def first(arguments: Mapping[str, object]) -> ToolResult:
        return ToolResult.success({"value": "first"})

    def second(arguments: Mapping[str, object]) -> ToolResult:
        assert any(
            event.event_type is EventType.TOOL_RESULT
            and event.payload.get("tool_call_id") == "first"
            for event in memory.events
        )
        return ToolResult.success({"value": "second"})

    for name, executor in (("first_tool", first), ("second_tool", second)):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                execute=executor,
            )
        )
    client = ScriptedClient(
        [
            ModelResponse(
                content=None,
                reasoning_content="private",
                finish_reason="tool_calls",
                usage=None,
                tool_calls=(
                    ToolCall(
                        id="first",
                        function=FunctionCall(name="first_tool", arguments="{}"),
                    ),
                    ToolCall(
                        id="second",
                        function=FunctionCall(name="second_tool", arguments="{}"),
                    ),
                ),
            )
        ]
    )

    AgentLoop(
        client=client,
        registry=registry,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=1,
        event_sink=memory,
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        clock=lambda: 0.0,
    ).run("task")


def test_write_argument_summaries_hash_content_without_retaining_it() -> None:
    create = summarize_tool_arguments(
        "create_file",
        f'{{"path":"safe.py","content":"{SECRET}"}}',
        sensitive_values=(SECRET,),
    )
    replace = summarize_tool_arguments(
        "replace_in_file",
        f'{{"path":"safe.py","old_text":"old {SECRET}","new_text":"new {SECRET}"}}',
        sensitive_values=(SECRET,),
    )

    assert create["path"] == "safe.py"
    assert create["content_bytes"] == len(SECRET.encode())
    assert "content" not in create
    assert "old_text" not in replace
    assert "new_text" not in replace
    assert SECRET not in str(create)
    assert SECRET not in str(replace)


def test_result_summaries_drop_file_and_command_bodies() -> None:
    read = summarize_tool_result(
        "read_file",
        "read-1",
        ToolResult.success(
            {
                "path": "safe.py",
                "content": f"1: {SECRET}",
                "total_lines": 1,
                "returned_line_count": 1,
                "returned_bytes": len(SECRET),
            }
        ),
        sensitive_values=(SECRET,),
    )
    command = summarize_tool_result(
        "run_command",
        "command-1",
        ToolResult.success(
            {
                "argv": ["pytest", f"TOKEN={SECRET}"],
                "cwd": ".",
                "command_kind": "test",
                "exit_code": 0,
                "stdout": SECRET,
                "stderr": SECRET,
                "stdout_bytes": 99,
                "stderr_bytes": 88,
                "stdout_truncated": True,
                "stderr_truncated": False,
                "timed_out": False,
            }
        ),
        sensitive_values=(SECRET,),
    )

    assert "content" not in read
    assert "stdout" not in command
    assert "stderr" not in command
    assert command["stdout_bytes"] == 99
    assert command["stdout_truncated"] is True
    assert SECRET not in str(command)


def test_diff_preview_and_error_message_are_redacted() -> None:
    result = ToolResult.success(
        {
            "path": "safe.py",
            "diff": f"+API_TOKEN={SECRET}\n",
            "diff_stats": {
                "added_lines": 1,
                "removed_lines": 0,
                "before_bytes": 0,
                "after_bytes": len(SECRET),
                "replacement_count": 0,
            },
        }
    )

    payload = diff_event_payload(
        "create_file",
        "create-1",
        result,
        sensitive_values=(SECRET,),
    )

    assert payload is not None
    assert SECRET not in str(payload)
    assert "[redacted]" in str(payload)


def test_payload_limits_drop_reasoning_environment_and_bound_json() -> None:
    payload = sanitize_payload(
        {
            "reasoning_content": SECRET,
            "environment": {"API_TOKEN": SECRET, "NORMAL": "value"},
            "API_TOKEN": SECRET,
            "text": "\x1b[31m" + "x" * 100_000 + "\x07",
            "items": list(range(1000)),
        },
        sensitive_values=(SECRET,),
    )
    event = RunEvent(
        run_id=RUN_ID,
        sequence=1,
        step=0,
        timestamp="2026-08-28T01:02:03.456789Z",
        event_type=EventType.MODEL,
        payload=payload,
    )

    serialized = event.to_json()
    assert len(serialized.encode()) <= MAX_EVENT_JSON_BYTES
    assert SECRET not in serialized
    assert "reasoning_content" not in serialized
    assert "environment" not in serialized
    assert "API_TOKEN" not in serialized
    assert "\\u001b" not in serialized
    assert "\\u0007" not in serialized
    assert payload["truncated"] is True


def test_integer_token_statistics_survive_without_weakening_secret_redaction() -> None:
    text = redact_text(
        "input_tokens=23916 output_tokens: 896 "
        f"API_TOKEN={SECRET} TOKEN=12345 SERVICE_SECRET={SECRET} "
        f"Authorization: Bearer {SECRET} input_tokens={SECRET}",
        sensitive_values=(SECRET,),
    )
    known_numeric_secret = redact_text("input_tokens=23916", sensitive_values=("23916",))
    payload = sanitize_payload(
        {
            "input_tokens": 23916,
            "output_tokens": 896,
            "API_TOKEN": SECRET,
            "input_tokens_text": SECRET,
        },
        sensitive_values=(SECRET,),
    )

    assert "input_tokens=23916" in text
    assert "output_tokens: 896" in text
    assert SECRET not in text
    assert "API_TOKEN=[redacted]" in text
    assert "TOKEN=[redacted]" in text
    assert "SERVICE_SECRET=[redacted]" in text
    assert "Authorization: Bearer [redacted]" in text
    assert "input_tokens=[redacted]" in text
    assert known_numeric_secret == "input_tokens=[redacted]"
    assert payload == {"input_tokens": 23916, "output_tokens": 896}
    assert type(payload["input_tokens"]) is int
    assert type(payload["output_tokens"]) is int


def test_terminal_sink_never_renders_same_event_twice() -> None:
    lines: list[str] = []
    terminal = TerminalSink(lines.append)
    event = RunEvent(
        run_id=RUN_ID,
        sequence=1,
        step=0,
        timestamp="2026-08-28T01:02:03.456789Z",
        event_type=EventType.TASK,
        payload={"task": "task"},
    )

    terminal.emit(event)
    terminal.emit(event)

    assert lines == ["TASK: task"]


def test_sink_failure_produces_one_safe_warning_without_recursion() -> None:
    class FailingSink:
        def emit(self, event: RunEvent) -> None:
            raise EventSinkError("TRACE_WRITE_ERROR", "safe failure")

    memory = MemorySink()
    emitter = EventEmitter(
        run_id=RUN_ID,
        sink=CompositeSink(FailingSink(), memory),
        clock=lambda: FIXED_TIME,
    )

    emitter.emit(EventType.TASK, step=0, payload={"task": "task"})
    emitter.emit(EventType.MODEL, step=1, payload={"text": "visible"})

    assert emitter.sink_error_codes == ("TRACE_WRITE_ERROR",)
    assert emitter.trace_complete is False
    warnings = [event for event in memory.events if event.event_type is EventType.WARNING]
    assert len(warnings) == 1
    assert warnings[0].payload["code"] == "TRACE_WRITE_ERROR"


def test_unknown_tool_arguments_store_only_keys_and_hash() -> None:
    summary = summarize_tool_arguments(
        "unknown",
        f'{{"payload":"{SECRET}","other":1}}',
        sensitive_values=(SECRET,),
    )

    assert summary["argument_keys"] == ["other", "payload"]
    assert "arguments_sha256" in summary
    assert SECRET not in str(summary)


def test_retry_protocol_repair_finish_and_termination_each_emit_events(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(create_finish_task_tool(tmp_path))
    finish = ToolCall(
        id="finish",
        function=FunctionCall(name="finish_task", arguments='{"summary":"done"}'),
    )
    client = ScriptedClient(
        [
            LLMRequestError("safe", category=LLMErrorCategory.CONNECTION),
            ModelResponse(
                content="need repair",
                reasoning_content="private",
                finish_reason="stop",
                usage=None,
            ),
            ModelResponse(
                content=None,
                reasoning_content="private",
                finish_reason="tool_calls",
                usage=None,
                tool_calls=(finish,),
            ),
        ]
    )
    memory = MemorySink()

    result = AgentLoop(
        client=client,
        registry=registry,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=3,
        event_sink=memory,
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
        random_value=lambda: 0.0,
    ).run("task")

    warning_codes = [
        event.payload["code"] for event in memory.events if event.event_type is EventType.WARNING
    ]
    assert warning_codes == ["API_RETRY", "PROTOCOL_REPAIR"]
    assert result.api_attempt_count == 3
    assert result.api_retry_count == 1
    assert any(event.event_type is EventType.COMPLETION for event in memory.events)
    assert memory.events[-1].event_type is EventType.TERMINATION
