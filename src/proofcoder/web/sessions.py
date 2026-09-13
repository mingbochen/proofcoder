"""Bounded in-process run sessions that expose one agent run to a local browser.

A session owns exactly one ``AgentLoop`` run executed on one worker thread. The
worker receives the same already sanitized events the trace recorder receives, so
the browser never observes anything the JSONL trace would not contain. The HTTP
layer only reads snapshots and buffered events through the locks defined here.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from proofcoder.agent_runtime import (
    AgentRunLimits,
    AgentRuntimeResources,
    build_agent_loop,
    create_agent_runtime_resources,
    emit_setup_termination,
    run_exit_code,
)
from proofcoder.config import ProofCoderConfig
from proofcoder.errors import ConfigurationError, ProofCoderError
from proofcoder.events import (
    EventEmitter,
    EventType,
    RunEvent,
    new_run_id,
)
from proofcoder.llm.base import LLMClient
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.protocol import CompletionStatus, RunResult, TerminationReason
from proofcoder.safety.secrets import sensitive_environment_values
from proofcoder.trace import TracePathError

MAX_BUFFERED_EVENTS = 4096
MAX_RETAINED_SESSIONS = 64
DEFAULT_MAX_ACTIVE_RUNS = 2
MAX_TASK_BYTES = 16 * 1024

ClientFactory = Callable[[ProofCoderConfig], LLMClient]
_DEFAULT_CLIENT_FACTORY: ClientFactory = DeepSeekClient


class SessionStatus(StrEnum):
    """Lifecycle of one browser-visible run."""

    RUNNING = "running"
    FINISHED = "finished"


class SessionError(Exception):
    """A stable, user-safe reason one run could not be started or addressed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Compact, JSON-ready facts about one run without its event bodies."""

    run_id: str
    workspace: str
    task: str
    status: SessionStatus
    started_at: str
    finished_at: str | None
    termination_reason: str | None
    completion_status: str | None
    exit_code: int | None
    changed_files: tuple[str, ...]
    cancel_requested: bool
    event_count: int
    dropped_events: int
    trace_path: str | None
    trace_complete: bool | None
    final_report: str | None

    def to_dict(self) -> dict[str, object]:
        """Return one deterministic mapping for the JSON API."""

        return {
            "run_id": self.run_id,
            "workspace": self.workspace,
            "task": self.task,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "termination_reason": self.termination_reason,
            "completion_status": self.completion_status,
            "exit_code": self.exit_code,
            "changed_files": list(self.changed_files),
            "cancel_requested": self.cancel_requested,
            "event_count": self.event_count,
            "dropped_events": self.dropped_events,
            "trace_path": self.trace_path,
            "trace_complete": self.trace_complete,
            "final_report": self.final_report,
        }


@dataclass(slots=True)
class RunSession:
    """One bounded run plus the buffered events a browser has not yet read."""

    run_id: str
    workspace: Path
    task: str
    limits: AgentRunLimits
    started_at: str
    status: SessionStatus = SessionStatus.RUNNING
    finished_at: str | None = None
    termination_reason: str | None = None
    completion_status: str | None = None
    exit_code: int | None = None
    changed_files: tuple[str, ...] = ()
    trace_path: str | None = None
    trace_complete: bool | None = None
    final_report: str | None = None
    _events: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=MAX_BUFFERED_EVENTS)
    )
    _appended: int = 0
    _dropped: int = 0
    _cancel: bool = False
    _condition: threading.Condition = field(default_factory=threading.Condition)

    @property
    def cancel_requested(self) -> bool:
        """Return whether a browser asked this run to stop."""

        with self._condition:
            return self._cancel

    @property
    def event_count(self) -> int:
        """Return how many events this run has produced so far."""

        with self._condition:
            return self._appended

    def request_cancel(self) -> bool:
        """Ask the loop to stop at its next bounded checkpoint."""

        with self._condition:
            if self.status is SessionStatus.FINISHED:
                return False
            already = self._cancel
            self._cancel = True
            self._condition.notify_all()
            return not already

    def append_event(self, event: RunEvent) -> None:
        """Buffer one already sanitized event and wake every waiting reader."""

        with self._condition:
            if len(self._events) == MAX_BUFFERED_EVENTS:
                self._dropped += 1
            self._events.append(event.to_dict())
            self._appended += 1
            self._condition.notify_all()

    def events_after(self, cursor: int) -> tuple[int, list[dict[str, object]]]:
        """Return buffered events after ``cursor`` plus the new cursor.

        The cursor counts appended events rather than trace sequence numbers so a
        reader that reconnects after the bounded buffer overflowed still advances.
        """

        with self._condition:
            return self._events_after_locked(cursor)

    def wait_for_events(
        self,
        cursor: int,
        timeout: float,
    ) -> tuple[int, list[dict[str, object]], bool]:
        """Block until new events, completion, or ``timeout`` elapses."""

        with self._condition:
            if cursor >= self._appended and self.status is SessionStatus.RUNNING:
                self._condition.wait(timeout)
            next_cursor, events = self._events_after_locked(cursor)
            finished = self.status is SessionStatus.FINISHED
            return next_cursor, events, finished and next_cursor >= self._appended

    def summary(self) -> SessionSummary:
        """Return one consistent snapshot taken under the session lock."""

        with self._condition:
            return SessionSummary(
                run_id=self.run_id,
                workspace=str(self.workspace),
                task=self.task,
                status=self.status,
                started_at=self.started_at,
                finished_at=self.finished_at,
                termination_reason=self.termination_reason,
                completion_status=self.completion_status,
                exit_code=self.exit_code,
                changed_files=self.changed_files,
                cancel_requested=self._cancel,
                event_count=self._appended,
                dropped_events=self._dropped,
                trace_path=self.trace_path,
                trace_complete=self.trace_complete,
                final_report=self.final_report,
            )

    def complete(
        self,
        *,
        termination_reason: TerminationReason,
        completion_status: CompletionStatus | None = None,
        changed_files: tuple[str, ...] = (),
        trace_path: str | None = None,
        trace_complete: bool | None = None,
        final_report: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        """Record the terminal outcome exactly once and release every reader."""

        with self._condition:
            if self.status is SessionStatus.FINISHED:
                return
            self.status = SessionStatus.FINISHED
            self.finished_at = finished_at or _timestamp()
            self.termination_reason = termination_reason.value
            self.completion_status = None if completion_status is None else completion_status.value
            self.exit_code = run_exit_code(termination_reason, completion_status)
            self.changed_files = changed_files
            self.trace_path = trace_path
            self.trace_complete = trace_complete
            self.final_report = final_report
            self._condition.notify_all()

    def _events_after_locked(self, cursor: int) -> tuple[int, list[dict[str, object]]]:
        bounded = max(0, min(cursor, self._appended))
        first_buffered = self._appended - len(self._events)
        start = max(0, bounded - first_buffered)
        return self._appended, list(self._events)[start:]


class _SessionSink:
    """Deliver already sanitized events into one session buffer."""

    def __init__(self, session: RunSession) -> None:
        self._session = session

    def emit(self, event: RunEvent) -> None:
        """Buffer one event for the browser without mutating it."""

        self._session.append_event(event)


class SessionManager:
    """Own every browser-started run, its worker thread, and its retention bound."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        client_factory: ClientFactory = _DEFAULT_CLIENT_FACTORY,
        max_active_runs: int = DEFAULT_MAX_ACTIVE_RUNS,
        max_retained_sessions: int = MAX_RETAINED_SESSIONS,
        run_id_factory: Callable[[], str] = new_run_id,
    ) -> None:
        if max_active_runs < 1:
            raise ValueError("max_active_runs must be at least 1")
        if max_retained_sessions < 1:
            raise ValueError("max_retained_sessions must be at least 1")
        self._environ = environ
        self._client_factory = client_factory
        self._max_active_runs = max_active_runs
        self._max_retained_sessions = max_retained_sessions
        self._run_id_factory = run_id_factory
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, RunSession] = OrderedDict()
        self._threads: dict[str, threading.Thread] = {}

    def start(self, *, workspace: Path, task: str, limits: AgentRunLimits) -> RunSession:
        """Validate one request, register the session, and start its worker thread."""

        cleaned = task.strip()
        if not cleaned:
            raise SessionError("EMPTY_TASK", "a task description is required")
        if len(cleaned.encode("utf-8")) > MAX_TASK_BYTES:
            raise SessionError(
                "TASK_TOO_LARGE",
                f"the task description must be at most {MAX_TASK_BYTES} bytes",
            )
        try:
            workspace_root = workspace.resolve(strict=True)
        except (OSError, RuntimeError):
            raise SessionError(
                "INVALID_WORKSPACE", "workspace must be an existing directory"
            ) from None
        if not workspace_root.is_dir():
            raise SessionError("INVALID_WORKSPACE", "workspace must be an existing directory")

        session = RunSession(
            run_id=self._run_id_factory(),
            workspace=workspace_root,
            task=cleaned,
            limits=limits,
            started_at=_timestamp(),
        )
        with self._lock:
            active = [
                item for item in self._sessions.values() if item.status is SessionStatus.RUNNING
            ]
            if any(item.workspace == workspace_root for item in active):
                raise SessionError(
                    "WORKSPACE_BUSY",
                    "another run is already using this workspace; wait for it or stop it",
                )
            if len(active) >= self._max_active_runs:
                raise SessionError(
                    "TOO_MANY_ACTIVE_RUNS",
                    f"at most {self._max_active_runs} runs may execute at the same time",
                )
            if session.run_id in self._sessions:
                raise SessionError("DUPLICATE_RUN_ID", "a run with this identifier already exists")
            self._sessions[session.run_id] = session
            self._prune_locked()
            thread = threading.Thread(
                target=self._execute,
                args=(session,),
                name=f"proofcoder-run-{session.run_id[:8]}",
                daemon=True,
            )
            self._threads[session.run_id] = thread
        thread.start()
        return session

    def get(self, run_id: str) -> RunSession | None:
        """Return one retained session, or None when it is unknown or evicted."""

        with self._lock:
            return self._sessions.get(run_id)

    def summaries(self) -> tuple[SessionSummary, ...]:
        """Return newest-first snapshots of every retained session."""

        with self._lock:
            sessions = list(self._sessions.values())
        return tuple(session.summary() for session in reversed(sessions))

    def workspace_busy(self, workspace: Path) -> bool:
        """Return whether one workspace currently has a run writing to it."""

        with self._lock:
            return any(
                item.workspace == workspace and item.status is SessionStatus.RUNNING
                for item in self._sessions.values()
            )

    def active_count(self) -> int:
        """Return how many retained sessions are still running."""

        with self._lock:
            return sum(
                1 for item in self._sessions.values() if item.status is SessionStatus.RUNNING
            )

    def cancel(self, run_id: str) -> bool:
        """Request cooperative cancellation for one running session."""

        session = self.get(run_id)
        if session is None:
            raise SessionError("RUN_NOT_FOUND", "no retained run has this identifier")
        return session.request_cancel()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel every running session and join its worker within ``timeout``."""

        with self._lock:
            sessions = list(self._sessions.values())
            threads = list(self._threads.values())
        for session in sessions:
            session.request_cancel()
        deadline = timeout
        for thread in threads:
            if deadline <= 0:
                break
            thread.join(deadline)

    def _prune_locked(self) -> None:
        """Drop the oldest finished sessions once retention is exceeded."""

        while len(self._sessions) > self._max_retained_sessions:
            for run_id, session in self._sessions.items():
                if session.status is SessionStatus.FINISHED:
                    del self._sessions[run_id]
                    self._threads.pop(run_id, None)
                    break
            else:
                return

    def _execute(self, session: RunSession) -> None:
        """Run one bounded agent loop and always record a terminal outcome."""

        sink = _SessionSink(session)
        sensitive_values = sensitive_environment_values(self._environ)
        try:
            resources = create_agent_runtime_resources(
                session.workspace,
                environ=self._environ,
                sensitive_values=sensitive_values,
                run_id_factory=lambda: session.run_id,
            )
        except (TracePathError, OSError, ValueError):
            _stream_termination(
                session=session,
                sink=sink,
                termination_reason=TerminationReason.INTERNAL_ERROR,
                sensitive_values=sensitive_values,
                include_task=True,
            )
            session.complete(termination_reason=TerminationReason.INTERNAL_ERROR)
            return
        if resources.checkpoint_error is not None:
            emit_setup_termination(
                task=session.task,
                resources=resources,
                termination_reason=TerminationReason.CHECKPOINT_ERROR,
                additional_sinks=(sink,),
                sensitive_values=sensitive_values,
            )
            resources.close()
            session.complete(termination_reason=TerminationReason.CHECKPOINT_ERROR)
            return

        try:
            try:
                config = ProofCoderConfig.from_env(environ=self._environ)
            except ConfigurationError:
                self._fail_before_loop(
                    session=session,
                    sink=sink,
                    resources=resources,
                    termination_reason=TerminationReason.CONFIGURATION_ERROR,
                    sensitive_values=sensitive_values,
                )
                return

            try:
                client = self._client_factory(config)
            except ProofCoderError:
                self._fail_before_loop(
                    session=session,
                    sink=sink,
                    resources=resources,
                    termination_reason=TerminationReason.API_ERROR,
                    sensitive_values=sensitive_values,
                )
                return

            try:
                result = build_agent_loop(
                    client=client,
                    resources=resources,
                    limits=session.limits,
                    additional_sinks=(sink,),
                    sensitive_values=sensitive_values,
                    cancel_requested=lambda: session.cancel_requested,
                ).run(session.task)
            except Exception:
                # AgentLoop converts its own failures into results, so reaching this
                # point means the trajectory is already incomplete: close it for the
                # browser without claiming the partial trace is usable evidence.
                _stream_termination(
                    session=session,
                    sink=sink,
                    termination_reason=TerminationReason.INTERNAL_ERROR,
                    sensitive_values=sensitive_values,
                    include_task=False,
                )
                session.complete(
                    termination_reason=TerminationReason.INTERNAL_ERROR,
                    trace_path=resources.recorder.trace_path,
                    trace_complete=False,
                )
                return
        finally:
            resources.close()

        _complete_from_result(session, result)

    def _fail_before_loop(
        self,
        *,
        session: RunSession,
        sink: _SessionSink,
        resources: AgentRuntimeResources,
        termination_reason: TerminationReason,
        sensitive_values: tuple[str, ...],
    ) -> None:
        """Persist and stream one minimal complete trajectory for a setup failure."""

        emit_setup_termination(
            task=session.task,
            resources=resources,
            termination_reason=termination_reason,
            additional_sinks=(sink,),
            sensitive_values=sensitive_values,
        )
        session.complete(
            termination_reason=termination_reason,
            trace_path=resources.recorder.trace_path,
            trace_complete=resources.recorder.trace_complete,
        )


def _complete_from_result(session: RunSession, result: RunResult) -> None:
    """Copy one terminated run result onto its session snapshot."""

    session.complete(
        termination_reason=result.termination_reason,
        completion_status=result.completion_status,
        changed_files=result.changed_files,
        trace_path=result.trace_path,
        trace_complete=result.trace_complete,
        final_report=result.final_report,
    )


def _stream_termination(
    *,
    session: RunSession,
    sink: _SessionSink,
    termination_reason: TerminationReason,
    sensitive_values: tuple[str, ...],
    include_task: bool,
) -> None:
    """Stream a browser-only termination when the trace cannot carry one."""

    emitter = EventEmitter(
        run_id=session.run_id,
        sink=sink,
        sensitive_values=sensitive_values,
    )
    if include_task:
        emitter.emit(EventType.TASK, step=0, payload={"task": session.task})
    emitter.emit(
        EventType.TERMINATION,
        step=0,
        payload={
            "api_attempts": 0,
            "api_retries": 0,
            "changed_files": [],
            "completion_status": "none",
            "context_compactions": 0,
            "elapsed_seconds": 0.0,
            "event_count": emitter.event_count + 1,
            "input_tokens": 0,
            "model_calls": 0,
            "output_tokens": 0,
            "termination_reason": termination_reason.value,
            "tool_calls": 0,
            "tool_errors": 0,
            "trace_complete": False,
            "trace_path": None,
            "verification": None,
            "warning_count": 0,
        },
    )


def _timestamp() -> str:
    """Return one UTC ISO-8601 timestamp with second precision."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
