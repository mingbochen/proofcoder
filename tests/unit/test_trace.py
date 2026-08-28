"""Offline JSONL recording, recovery, path safety, and trace CLI tests."""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

import proofcoder.cli as cli
from proofcoder.agent import AgentLoop
from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.events import CompositeSink, EventEmitter, EventType, MemorySink, RunEvent
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, TerminationReason, ToolCall
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import create_create_file_tool, create_replace_in_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry
from proofcoder.trace import (
    TRACE_FILENAME,
    TracePathError,
    TraceRecorder,
    list_traces,
    read_trace,
)

RUN_ID = "b" * 32
SECOND_RUN_ID = "c" * 32
SECRET = "obviously-fake-trace-secret"
FIXED_TIME = datetime(2026, 8, 28, 2, 3, 4, 567890, tzinfo=UTC)


def _response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=f"private reasoning {SECRET}",
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=tuple(calls),
    )


def _call(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=arguments))


def _probe_registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="probe",
            description="Return a stable observation.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=lambda arguments: ToolResult.success({"value": 1}),
        )
    )
    registry.register(create_finish_task_tool(workspace))
    return registry


def _record_complete_trace(workspace: Path, run_id: str) -> Path:
    recorder = TraceRecorder(workspace, run_id, sensitive_values=(SECRET,))
    emitter = EventEmitter(
        run_id=run_id,
        sink=recorder,
        clock=lambda: FIXED_TIME,
        sensitive_values=(SECRET,),
    )
    emitter.emit(EventType.TASK, step=0, payload={"task": f"task {SECRET}"})
    emitter.emit(
        EventType.TERMINATION,
        step=0,
        payload={
            "termination_reason": "max_steps",
            "completion_status": "none",
            "changed_files": [],
            "elapsed_seconds": 12.345,
            "input_tokens": 23916,
            "output_tokens": 896,
            "trace_complete": True,
        },
    )
    recorder.close()
    return workspace / Path(recorder.trace_path)


def test_recorder_writes_parseable_utf8_lf_and_flushes_each_event(tmp_path: Path) -> None:
    path = _record_complete_trace(tmp_path, RUN_ID)

    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\r\n" not in raw
    lines = raw.splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert SECRET.encode() not in raw
    result = read_trace(tmp_path, RUN_ID)
    assert result.trace_complete is True
    assert [event.sequence for event in result.events] == [1, 2]
    assert result.trace_path == f".proofcoder/runs/{RUN_ID}/{TRACE_FILENAME}"
    termination = result.events[-1]
    assert type(termination.payload["input_tokens"]) is int
    assert type(termination.payload["output_tokens"]) is int
    assert termination.payload["input_tokens"] == 23916
    assert termination.payload["output_tokens"] == 896


def test_recorder_resanitizes_direct_events_and_drops_forbidden_fields(tmp_path: Path) -> None:
    recorder = TraceRecorder(tmp_path, RUN_ID, sensitive_values=(SECRET,))
    recorder.emit(
        RunEvent(
            run_id=RUN_ID,
            sequence=1,
            step=0,
            timestamp="2026-08-28T02:03:04.567890Z",
            event_type=EventType.MODEL,
            payload={
                "text": f"visible {SECRET}",
                "reasoning_content": SECRET,
                "stdout": SECRET,
                "environment": {"API_TOKEN": SECRET},
            },
        )
    )
    recorder.close()

    raw = (tmp_path / Path(recorder.trace_path)).read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "reasoning_content" not in raw
    assert "stdout" not in raw
    assert "environment" not in raw


@pytest.mark.parametrize(
    ("responses", "max_steps", "reason"),
    [
        (
            [_response(content="one"), _response(content="two")],
            4,
            TerminationReason.MODEL_STOPPED,
        ),
        ([_response(_call("probe", "probe"))], 1, TerminationReason.MAX_STEPS),
        (
            [LLMRequestError("safe", category=LLMErrorCategory.PERMANENT)],
            4,
            TerminationReason.API_ERROR,
        ),
        (
            [
                _response(_call("one", "probe")),
                _response(_call("two", "probe")),
                _response(_call("three", "probe")),
            ],
            4,
            TerminationReason.NO_PROGRESS,
        ),
        (
            [
                _response(
                    _call(
                        "finish",
                        "finish_task",
                        '{"summary":"blocked","blocked_reason":"input unavailable"}',
                    )
                )
            ],
            4,
            TerminationReason.FINISH_TASK,
        ),
    ],
)
def test_controlled_agent_terminations_end_with_trace_event(
    tmp_path: Path,
    responses: list[ModelResponse | LLMRequestError],
    max_steps: int,
    reason: TerminationReason,
) -> None:
    recorder = TraceRecorder(tmp_path, RUN_ID)
    result = AgentLoop(
        client=ScriptedClient(responses),
        registry=_probe_registry(tmp_path),
        workspace=tmp_path,
        system_prompt="system",
        max_steps=max_steps,
        max_api_attempts=1,
        clock=lambda: 0.0,
        event_clock=lambda: FIXED_TIME,
        event_sink=recorder,
        run_id_factory=lambda: RUN_ID,
        trace_path=recorder.trace_path,
    ).run("task")
    recorder.close()

    trace = read_trace(tmp_path, RUN_ID)
    assert result.termination_reason is reason
    assert trace.events[-1].event_type is EventType.TERMINATION
    assert trace.events[-1].payload["termination_reason"] == reason.value
    assert trace.trace_complete is True
    if reason is TerminationReason.FINISH_TASK:
        assert any(event.event_type is EventType.COMPLETION for event in trace.events)


def test_keyboard_interrupt_keeps_prior_events_and_termination(tmp_path: Path) -> None:
    class InterruptingClient:
        def complete(self, messages: object, tools: object = ()) -> ModelResponse:
            raise KeyboardInterrupt

    recorder = TraceRecorder(tmp_path, RUN_ID)
    result = AgentLoop(
        client=InterruptingClient(),  # type: ignore[arg-type]
        registry=_probe_registry(tmp_path),
        workspace=tmp_path,
        system_prompt="system",
        max_steps=2,
        event_sink=recorder,
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        clock=lambda: 0.0,
        trace_path=recorder.trace_path,
    ).run("task")
    recorder.close()

    trace = read_trace(tmp_path, RUN_ID)
    assert result.termination_reason is TerminationReason.INTERRUPTED
    assert [event.event_type for event in trace.events] == [
        EventType.TASK,
        EventType.TERMINATION,
    ]


def test_unverified_finish_has_completion_and_termination_trace(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(create_create_file_tool(tmp_path))
    registry.register(create_finish_task_tool(tmp_path))
    client = ScriptedClient(
        [
            _response(
                _call(
                    "create",
                    "create_file",
                    '{"path":"created.txt","content":"value"}',
                )
            ),
            _response(
                _call(
                    "finish",
                    "finish_task",
                    '{"summary":"created","changed_files":["created.txt"]}',
                )
            ),
        ]
    )
    recorder = TraceRecorder(tmp_path, RUN_ID)

    result = AgentLoop(
        client=client,
        registry=registry,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=3,
        event_sink=recorder,
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        clock=lambda: 0.0,
        trace_path=recorder.trace_path,
    ).run("create")
    recorder.close()
    trace = read_trace(tmp_path, RUN_ID)

    assert result.completion_status is not None
    assert result.completion_status.value == "completed_unverified"
    assert trace.events[-2].event_type is EventType.COMPLETION
    assert trace.events[-1].event_type is EventType.TERMINATION
    assert trace.events[-1].payload["completion_status"] == "completed_unverified"


def test_failure_edit_success_finish_trace_is_locally_verified_and_replayable(
    tmp_path: Path,
) -> None:
    (tmp_path / "subject.py").write_text('VALUE = "broken"\n', encoding="utf-8")
    (tmp_path / "test_subject.py").write_text(
        "import unittest\n"
        "import subject\n\n"
        "class SubjectTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(subject.VALUE, 'fixed')\n",
        encoding="utf-8",
    )
    environment = {
        "PATH": str(Path(sys.executable).resolve().parent),
        "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
    }
    registry = ToolRegistry()
    registry.register(create_replace_in_file_tool(tmp_path))
    registry.register(create_run_command_tool(tmp_path, environ=environment))
    registry.register(create_finish_task_tool(tmp_path))
    verify = ["python", "-m", "unittest", "-q"]
    client = ScriptedClient(
        [
            _response(
                _call(
                    "failing",
                    "run_command",
                    json.dumps({"argv": verify, "timeout_seconds": 10}),
                )
            ),
            _response(
                _call(
                    "edit",
                    "replace_in_file",
                    '{"path":"subject.py","old_text":"broken","new_text":"fixed"}',
                )
            ),
            _response(
                _call(
                    "passing",
                    "run_command",
                    json.dumps({"argv": verify, "timeout_seconds": 10}),
                )
            ),
            _response(
                _call(
                    "finish",
                    "finish_task",
                    json.dumps(
                        {
                            "summary": "fixed",
                            "changed_files": ["subject.py"],
                            "verification_command": verify,
                        }
                    ),
                )
            ),
        ]
    )
    recorder = TraceRecorder(tmp_path, RUN_ID)

    result = AgentLoop(
        client=client,
        registry=registry,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=6,
        event_sink=recorder,
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        trace_path=recorder.trace_path,
    ).run("fix and verify")
    recorder.close()
    trace = read_trace(tmp_path, RUN_ID)

    assert result.completion_status is not None
    assert result.completion_status.value == "completed_verified"
    assert result.changed_files == ("subject.py",)
    assert result.verification_exit_code == 0
    verifications = [event for event in trace.events if event.event_type is EventType.VERIFICATION]
    assert [event.payload["exit_code"] for event in verifications] == [1, 0]
    assert [event.payload["accepted"] for event in verifications] == [False, True]
    assert any(event.event_type is EventType.DIFF for event in trace.events)
    assert trace.events[-2].event_type is EventType.COMPLETION
    assert trace.events[-1].event_type is EventType.TERMINATION
    assert "exit_code=1" in result.final_report
    assert "exit_code=0" in result.final_report
    assert all(
        isinstance(json.loads(line), dict)
        for line in (tmp_path / Path(result.trace_path or ""))
        .read_text(encoding="utf-8")
        .splitlines()
    )


def test_trace_write_failure_warns_once_and_marks_run_incomplete(tmp_path: Path) -> None:
    class FailingStream:
        def write(self, value: bytes) -> int:
            raise OSError("private write failure")

        def flush(self) -> None:
            raise AssertionError("flush must not follow failed write")

        def close(self) -> None:
            return None

    recorder = TraceRecorder(tmp_path, RUN_ID)
    assert recorder._stream is not None
    recorder._stream.close()
    recorder._stream = FailingStream()  # type: ignore[assignment]
    memory = MemorySink()
    result = AgentLoop(
        client=ScriptedClient([_response(content="one"), _response(content="two")]),
        registry=_probe_registry(tmp_path),
        workspace=tmp_path,
        system_prompt="system",
        max_steps=3,
        event_sink=CompositeSink(memory, recorder),
        run_id_factory=lambda: RUN_ID,
        event_clock=lambda: FIXED_TIME,
        clock=lambda: 0.0,
        trace_path=recorder.trace_path,
    ).run("task")

    warnings = [
        event
        for event in memory.events
        if event.event_type is EventType.WARNING
        and event.payload.get("code") == "TRACE_WRITE_ERROR"
    ]
    assert len(warnings) == 1
    assert result.trace_complete is False
    assert result.warnings.count("TRACE_WRITE_ERROR") == 1
    assert "private write failure" not in result.final_report


def test_each_run_id_gets_an_independent_trace(tmp_path: Path) -> None:
    first = _record_complete_trace(tmp_path, RUN_ID)
    second = _record_complete_trace(tmp_path, SECOND_RUN_ID)

    assert first != second
    summaries = list_traces(tmp_path)
    assert [summary.run_id for summary in summaries] == [RUN_ID, SECOND_RUN_ID]
    assert all(summary.event_count == 2 for summary in summaries)
    assert all(summary.trace_complete for summary in summaries)


def test_external_proofcoder_symlink_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    try:
        (workspace / ".proofcoder").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(TracePathError) as captured:
        TraceRecorder(workspace, RUN_ID)

    assert captured.value.code == "TRACE_PATH_UNSAFE"
    assert not (outside / "runs").exists()


def test_invalid_and_external_run_ids_are_rejected(tmp_path: Path) -> None:
    for run_id in ("../outside", "/absolute", "A" * 32, "short"):
        with pytest.raises(TracePathError, match="32 lowercase"):
            read_trace(tmp_path, run_id)


def test_malformed_unknown_schema_missing_termination_and_tail_are_safe(tmp_path: Path) -> None:
    path = _record_complete_trace(tmp_path, RUN_ID)
    task = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    unknown = dict(task)
    unknown["sequence"] = 2
    unknown["schema_version"] = 999
    path.write_bytes(
        (json.dumps(task, separators=(",", ":")) + "\n").encode()
        + (json.dumps(unknown, separators=(",", ":")) + "\n").encode()
        + b"{malformed}\n"
        + b'{"schema_version":1'
    )

    result = read_trace(tmp_path, RUN_ID)

    codes = {issue.code for issue in result.issues}
    assert {
        "UNKNOWN_SCHEMA",
        "MALFORMED_JSONL",
        "TRUNCATED_TAIL",
        "MISSING_TERMINATION",
    } <= codes
    assert result.trace_complete is False
    assert len(result.events) == 1


def test_trace_cli_list_and_show_are_read_only_and_need_no_configuration(tmp_path: Path) -> None:
    _record_complete_trace(tmp_path, RUN_ID)
    list_stream = io.StringIO()
    show_stream = io.StringIO()

    list_code = cli.main(
        ["trace", "list", "--workspace", str(tmp_path)],
        environ={},
        cwd=tmp_path,
        console=Console(file=list_stream, force_terminal=False, color_system=None),
        client_factory=lambda config: (_ for _ in ()).throw(AssertionError(config)),
        run_client_factory=lambda config: (_ for _ in ()).throw(AssertionError(config)),
    )
    show_code = cli.main(
        ["trace", "show", "--workspace", str(tmp_path), RUN_ID],
        environ={},
        cwd=tmp_path,
        console=Console(file=show_stream, force_terminal=False, color_system=None),
        client_factory=lambda config: (_ for _ in ()).throw(AssertionError(config)),
        run_client_factory=lambda config: (_ for _ in ()).throw(AssertionError(config)),
    )

    assert list_code == 0
    assert RUN_ID in list_stream.getvalue()
    assert "max_steps" in list_stream.getvalue()
    assert show_code == 0
    assert "TASK: task [redacted]" in show_stream.getvalue()
    assert "DONE: termination=max_steps" in show_stream.getvalue()
    assert "REPORT:" in show_stream.getvalue()
    assert show_stream.getvalue().count("input_tokens=23916") == 2
    assert show_stream.getvalue().count("output_tokens=896") == 2
    assert SECRET not in show_stream.getvalue()


def test_trace_cli_reports_missing_invalid_and_incomplete_runs(tmp_path: Path) -> None:
    path = _record_complete_trace(tmp_path, RUN_ID)
    path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None)

    incomplete = cli.main(
        ["trace", "show", "--workspace", str(tmp_path), RUN_ID],
        environ={},
        cwd=tmp_path,
        console=console,
    )
    missing = cli.main(
        ["trace", "show", "--workspace", str(tmp_path), SECOND_RUN_ID],
        environ={},
        cwd=tmp_path,
        console=console,
    )
    invalid = cli.main(
        ["trace", "show", "--workspace", str(tmp_path), "../outside"],
        environ={},
        cwd=tmp_path,
        console=console,
    )

    assert incomplete == 1
    assert missing == 1
    assert invalid == 1
    output = stream.getvalue()
    assert "MISSING_TERMINATION" in output
    assert "TRACE_NOT_FOUND" in output
    assert "INVALID_RUN_ID" in output
