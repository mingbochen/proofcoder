"""Offline tests for the browser-facing run sessions.

Every test drives the real ``SessionManager`` with a scripted or deliberately
failing client, so the buffering, cancellation, retention, and termination paths
are exercised against the same agent runtime the command line uses.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from proofcoder.agent_runtime import AgentRunLimits
from proofcoder.config import ProofCoderConfig
from proofcoder.errors import DeepSeekAPIError
from proofcoder.events import EventType, RunEvent
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import (
    CompletionStatus,
    FunctionCall,
    ModelResponse,
    TerminationReason,
    ToolCall,
)
from proofcoder.trace import list_traces, read_trace
from proofcoder.web.sessions import (
    MAX_BUFFERED_EVENTS,
    MAX_TASK_BYTES,
    RunSession,
    SessionError,
    SessionManager,
    SessionStatus,
)

SENSITIVE_SENTINEL = "never-echo-this-session-value"
REASONING = "hidden-reasoning-must-stay-private"
ENVIRON = {"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL}


def _response(*, content: str | None = None, calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=REASONING,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments)))


def _create_and_finish() -> list[ModelResponse]:
    return [
        _response(
            content="Creating the file.",
            calls=(_call("create-1", "create_file", {"path": "made.py", "content": "x = 1\n"}),),
        ),
        _response(
            content="Reporting completion.",
            calls=(
                _call(
                    "finish-1",
                    "finish_task",
                    {"summary": "created made.py", "changed_files": ["made.py"]},
                ),
            ),
        ),
    ]


def _wait_finished(session: RunSession, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session.summary().status is SessionStatus.FINISHED:
            return
        time.sleep(0.01)
    raise AssertionError("the session did not finish within the test timeout")


def _event(sequence: int) -> RunEvent:
    return RunEvent(
        run_id="a" * 32,
        sequence=sequence,
        step=1,
        timestamp="2026-01-01T00:00:00Z",
        event_type=EventType.MODEL,
        payload={"text": f"event-{sequence}"},
    )


def _manager(responses: Sequence[ModelResponse], **kwargs: object) -> SessionManager:
    scripted = ScriptedClient(list(responses))
    return SessionManager(
        environ=ENVIRON,
        client_factory=lambda config: scripted,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------- buffering ----------


def test_events_after_returns_only_newer_events() -> None:
    session = RunSession(
        run_id="b" * 32,
        workspace=Path("."),
        task="buffer",
        limits=AgentRunLimits(),
        started_at="2026-01-01T00:00:00Z",
    )

    session.append_event(_event(1))
    session.append_event(_event(2))
    cursor, events = session.events_after(0)

    assert cursor == 2
    assert [item["sequence"] for item in events] == [1, 2]
    assert session.events_after(2) == (2, [])
    assert session.events_after(99)[1] == []


def test_buffer_overflow_counts_drops_and_keeps_the_cursor_monotonic() -> None:
    session = RunSession(
        run_id="c" * 32,
        workspace=Path("."),
        task="overflow",
        limits=AgentRunLimits(),
        started_at="2026-01-01T00:00:00Z",
    )

    for index in range(MAX_BUFFERED_EVENTS + 3):
        session.append_event(_event(index + 1))
    summary = session.summary()
    cursor, events = session.events_after(0)

    assert summary.dropped_events == 3
    assert summary.event_count == MAX_BUFFERED_EVENTS + 3
    assert cursor == MAX_BUFFERED_EVENTS + 3
    assert len(events) == MAX_BUFFERED_EVENTS


def test_wait_for_events_returns_immediately_when_the_cursor_is_behind() -> None:
    session = RunSession(
        run_id="d" * 32,
        workspace=Path("."),
        task="wait",
        limits=AgentRunLimits(),
        started_at="2026-01-01T00:00:00Z",
    )
    session.append_event(_event(1))

    started = time.monotonic()
    cursor, events, done = session.wait_for_events(0, 5.0)

    assert time.monotonic() - started < 4.0
    assert cursor == 1
    assert len(events) == 1
    assert done is False


def test_wait_for_events_wakes_on_completion() -> None:
    session = RunSession(
        run_id="e" * 32,
        workspace=Path("."),
        task="wait",
        limits=AgentRunLimits(),
        started_at="2026-01-01T00:00:00Z",
    )

    def finish_soon() -> None:
        time.sleep(0.05)
        session.complete(termination_reason=TerminationReason.MODEL_STOPPED)

    thread = threading.Thread(target=finish_soon)
    thread.start()
    cursor, events, done = session.wait_for_events(0, 5.0)
    thread.join()

    assert cursor == 0
    assert events == []
    assert done is True


def test_complete_is_recorded_once_and_maps_the_exit_code() -> None:
    session = RunSession(
        run_id="f" * 32,
        workspace=Path("."),
        task="complete",
        limits=AgentRunLimits(),
        started_at="2026-01-01T00:00:00Z",
    )

    session.complete(
        termination_reason=TerminationReason.FINISH_TASK,
        completion_status=CompletionStatus.COMPLETED_UNVERIFIED,
        changed_files=("made.py",),
    )
    session.complete(termination_reason=TerminationReason.MAX_STEPS)
    summary = session.summary()

    assert summary.status is SessionStatus.FINISHED
    assert summary.termination_reason == "finish_task"
    assert summary.exit_code == 3
    assert summary.changed_files == ("made.py",)
    assert session.request_cancel() is False


def test_cancel_is_reported_once_while_running() -> None:
    session = RunSession(
        run_id="0" * 32,
        workspace=Path("."),
        task="cancel",
        limits=AgentRunLimits(),
        started_at="2026-01-01T00:00:00Z",
    )

    assert session.request_cancel() is True
    assert session.request_cancel() is False
    assert session.cancel_requested is True


# ---------- manager validation ----------


@pytest.mark.parametrize("task", ["", "   ", "\n"])
def test_blank_tasks_are_rejected(tmp_path: Path, task: str) -> None:
    manager = _manager([])

    with pytest.raises(SessionError) as error:
        manager.start(workspace=tmp_path, task=task, limits=AgentRunLimits())

    assert error.value.code == "EMPTY_TASK"


def test_oversized_tasks_are_rejected(tmp_path: Path) -> None:
    manager = _manager([])

    with pytest.raises(SessionError) as error:
        manager.start(
            workspace=tmp_path,
            task="x" * (MAX_TASK_BYTES + 1),
            limits=AgentRunLimits(),
        )

    assert error.value.code == "TASK_TOO_LARGE"


def test_missing_workspace_is_rejected(tmp_path: Path) -> None:
    manager = _manager([])

    with pytest.raises(SessionError) as error:
        manager.start(workspace=tmp_path / "absent", task="task", limits=AgentRunLimits())

    assert error.value.code == "INVALID_WORKSPACE"


def test_file_workspace_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("not a directory", encoding="utf-8")
    manager = _manager([])

    with pytest.raises(SessionError) as error:
        manager.start(workspace=target, task="task", limits=AgentRunLimits())

    assert error.value.code == "INVALID_WORKSPACE"


def test_cancelling_an_unknown_run_is_reported(tmp_path: Path) -> None:
    manager = _manager([])

    with pytest.raises(SessionError) as error:
        manager.cancel("1" * 32)

    assert error.value.code == "RUN_NOT_FOUND"
    assert manager.get("1" * 32) is None


def test_invalid_manager_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        SessionManager(max_active_runs=0)
    with pytest.raises(ValueError):
        SessionManager(max_retained_sessions=0)


# ---------- full runs ----------


def test_a_scripted_run_streams_events_and_writes_its_trace(tmp_path: Path) -> None:
    manager = _manager(_create_and_finish())

    session = manager.start(workspace=tmp_path, task="create made.py", limits=AgentRunLimits())
    _wait_finished(session)
    summary = session.summary()
    _, events = session.events_after(0)

    assert summary.termination_reason == "finish_task"
    assert summary.completion_status == "completed_unverified"
    assert summary.exit_code == 3
    assert summary.changed_files == ("made.py",)
    assert summary.trace_complete is True
    assert (tmp_path / "made.py").read_text(encoding="utf-8") == "x = 1\n"

    types = [item["event_type"] for item in events]
    assert types[0] == "task"
    assert types[-1] == "termination"
    serialized = json.dumps(events, ensure_ascii=False)
    assert REASONING not in serialized
    assert SENSITIVE_SENTINEL not in serialized

    stored = list_traces(tmp_path)
    assert [item.run_id for item in stored] == [session.run_id]
    assert len(read_trace(tmp_path, session.run_id).events) == len(events)


def test_a_second_run_in_the_same_workspace_is_refused_while_one_is_active(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    class _BlockingClient:
        def complete(self, messages: object, tools: object = ()) -> ModelResponse:
            release.wait(10)
            return _response(content="released")

    manager = SessionManager(environ=ENVIRON, client_factory=lambda config: _BlockingClient())
    session = manager.start(workspace=tmp_path, task="first", limits=AgentRunLimits(max_steps=1))
    try:
        with pytest.raises(SessionError) as error:
            manager.start(workspace=tmp_path, task="second", limits=AgentRunLimits())
        assert error.value.code == "WORKSPACE_BUSY"
    finally:
        release.set()
    _wait_finished(session)

    assert manager.active_count() == 0


def test_concurrent_run_limit_is_enforced_across_workspaces(tmp_path: Path) -> None:
    release = threading.Event()
    first = tmp_path / "one"
    second = tmp_path / "two"
    for directory in (first, second):
        directory.mkdir()

    class _BlockingClient:
        def complete(self, messages: object, tools: object = ()) -> ModelResponse:
            release.wait(10)
            return _response(content="released")

    manager = SessionManager(
        environ=ENVIRON,
        client_factory=lambda config: _BlockingClient(),
        max_active_runs=1,
    )
    session = manager.start(workspace=first, task="first", limits=AgentRunLimits(max_steps=1))
    try:
        with pytest.raises(SessionError) as error:
            manager.start(workspace=second, task="second", limits=AgentRunLimits())
        assert error.value.code == "TOO_MANY_ACTIVE_RUNS"
    finally:
        release.set()
    _wait_finished(session)


def test_cancel_stops_a_running_session(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class _GatedClient:
        def complete(self, messages: object, tools: object = ()) -> ModelResponse:
            started.set()
            release.wait(10)
            return _response(content="no tool calls")

    manager = SessionManager(environ=ENVIRON, client_factory=lambda config: _GatedClient())
    session = manager.start(workspace=tmp_path, task="cancel me", limits=AgentRunLimits())
    assert started.wait(10)
    assert manager.cancel(session.run_id) is True
    release.set()
    _wait_finished(session)
    summary = session.summary()

    assert summary.termination_reason == "interrupted"
    assert summary.exit_code == 130
    assert summary.cancel_requested is True


def test_shutdown_cancels_and_joins_running_sessions(tmp_path: Path) -> None:
    started = threading.Event()

    class _PollingClient:
        def complete(self, messages: object, tools: object = ()) -> ModelResponse:
            started.set()
            return _response(content="no tool calls")

    manager = SessionManager(environ=ENVIRON, client_factory=lambda config: _PollingClient())
    session = manager.start(workspace=tmp_path, task="shut down", limits=AgentRunLimits())
    assert started.wait(10)
    manager.shutdown(timeout=10.0)
    _wait_finished(session)

    assert manager.active_count() == 0


def test_missing_credentials_end_the_run_with_a_configuration_termination(
    tmp_path: Path,
) -> None:
    manager = SessionManager(environ={}, client_factory=lambda config: ScriptedClient([]))

    session = manager.start(workspace=tmp_path, task="no key", limits=AgentRunLimits())
    _wait_finished(session)
    summary = session.summary()
    _, events = session.events_after(0)

    assert summary.termination_reason == "configuration_error"
    assert summary.exit_code == 1
    # The baseline is captured before configuration is read, so the trace of a run
    # that stops during setup still records that a checkpoint exists for it.
    assert [item["event_type"] for item in events] == ["task", "checkpoint", "termination"]
    assert list_traces(tmp_path)[0].run_id == session.run_id


def test_a_failing_client_factory_ends_the_run_with_an_api_termination(tmp_path: Path) -> None:
    def failing_factory(config: ProofCoderConfig) -> object:
        raise DeepSeekAPIError("client construction failed")

    manager = SessionManager(environ=ENVIRON, client_factory=failing_factory)  # type: ignore[arg-type]

    session = manager.start(workspace=tmp_path, task="bad client", limits=AgentRunLimits())
    _wait_finished(session)
    summary = session.summary()

    assert summary.termination_reason == "api_error"
    assert summary.exit_code == 1


def test_an_unexpected_loop_failure_is_reported_as_an_incomplete_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.web.sessions as sessions_module

    def exploding_loop(**kwargs: object) -> object:
        class _Loop:
            def run(self, task: str) -> object:
                raise RuntimeError("unexpected")

        return _Loop()

    monkeypatch.setattr(sessions_module, "build_agent_loop", exploding_loop)
    manager = _manager([])

    session = manager.start(workspace=tmp_path, task="explode", limits=AgentRunLimits())
    _wait_finished(session)
    summary = session.summary()
    _, events = session.events_after(0)

    assert summary.termination_reason == "internal_error"
    assert summary.trace_complete is False
    assert [item["event_type"] for item in events][-1] == "termination"


def test_a_failing_trace_recorder_still_streams_a_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.web.sessions as sessions_module

    def exploding_resources(*args: object, **kwargs: object) -> object:
        raise OSError("no runtime directory")

    monkeypatch.setattr(sessions_module, "create_agent_runtime_resources", exploding_resources)
    manager = _manager([])

    session = manager.start(workspace=tmp_path, task="no trace", limits=AgentRunLimits())
    _wait_finished(session)
    summary = session.summary()
    _, events = session.events_after(0)

    assert summary.termination_reason == "internal_error"
    assert summary.trace_path is None
    assert [item["event_type"] for item in events] == ["task", "termination"]


def test_finished_sessions_are_evicted_once_retention_is_exceeded(tmp_path: Path) -> None:
    manager = SessionManager(
        environ=ENVIRON,
        client_factory=lambda config: ScriptedClient([_response(content="stop")] * 3),
        max_retained_sessions=2,
    )
    run_ids: list[str] = []
    for index in range(3):
        workspace = tmp_path / f"ws{index}"
        workspace.mkdir()
        session = manager.start(
            workspace=workspace,
            task=f"task {index}",
            limits=AgentRunLimits(max_steps=1),
        )
        _wait_finished(session)
        run_ids.append(session.run_id)

    summaries = manager.summaries()

    assert len(summaries) == 2
    assert manager.get(run_ids[0]) is None
    assert [item.run_id for item in summaries] == [run_ids[2], run_ids[1]]


def test_duplicate_run_identifiers_are_rejected(tmp_path: Path) -> None:
    fixed = "9" * 32
    manager = SessionManager(
        environ=ENVIRON,
        client_factory=lambda config: ScriptedClient([_response(content="stop")] * 4),
        run_id_factory=lambda: fixed,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        directory.mkdir()
    session = manager.start(workspace=first, task="one", limits=AgentRunLimits(max_steps=1))
    _wait_finished(session)

    with pytest.raises(SessionError) as error:
        manager.start(workspace=second, task="two", limits=AgentRunLimits(max_steps=1))

    assert error.value.code == "DUPLICATE_RUN_ID"


def test_summary_serialisation_is_json_ready(tmp_path: Path) -> None:
    manager = _manager(_create_and_finish())
    session = manager.start(workspace=tmp_path, task="serialise", limits=AgentRunLimits())
    _wait_finished(session)

    body = session.summary().to_dict()

    assert json.loads(json.dumps(body))["run_id"] == session.run_id
    assert body["changed_files"] == ["made.py"]
    assert body["status"] == "finished"
