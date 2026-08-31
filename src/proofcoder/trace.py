"""Flushed JSONL trace recording and safe read-only trace inspection."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from proofcoder.events import (
    MAX_EVENT_JSON_BYTES,
    SCHEMA_VERSION,
    EventSinkError,
    EventType,
    RunEvent,
    sanitize_payload,
)

RUN_ID_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")
TRACE_RELATIVE_ROOT = Path(".proofcoder/runs")
TRACE_FILENAME = "trace.jsonl"
MAX_TRACE_LINE_BYTES = MAX_EVENT_JSON_BYTES + 1


class TracePathError(Exception):
    """A stable trace path or run-id failure safe to display."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TraceIssue:
    """One bounded, non-content-bearing trace parsing issue."""

    code: str
    line_number: int | None
    message: str


@dataclass(frozen=True, slots=True)
class TraceReadResult:
    """Safe events and diagnostics recovered from one JSONL file."""

    run_id: str
    trace_path: str
    events: tuple[RunEvent, ...]
    issues: tuple[TraceIssue, ...]
    trace_complete: bool


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """Compact facts used by ``proofcoder trace list``."""

    run_id: str
    started_at: str
    status: str
    event_count: int
    trace_complete: bool


def validate_run_id(run_id: str) -> str:
    """Reject paths, traversal, and every non-local run-id form."""

    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise TracePathError(
            "INVALID_RUN_ID",
            "run_id must be exactly 32 lowercase hexadecimal characters",
        )
    return run_id


class TraceRecorder:
    """Write one deterministic UTF-8/LF JSON object and flush per event."""

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        *,
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self._workspace = workspace.resolve(strict=True)
        if not self._workspace.is_dir():
            raise TracePathError("INVALID_WORKSPACE", "workspace must be an existing directory")
        self.run_id = validate_run_id(run_id)
        self._sensitive_values = sensitive_values
        self._disabled = False
        self._trace_complete = True
        run_directory = _create_run_directory(self._workspace, self.run_id)
        self._path = run_directory / TRACE_FILENAME
        try:
            self._stream: BinaryIO | None = self._path.open("xb")
        except OSError:
            raise TracePathError(
                "TRACE_PATH_UNAVAILABLE",
                "trace file could not be created safely inside the workspace",
            ) from None

    @property
    def trace_path(self) -> str:
        """Return the workspace-relative POSIX trace path."""

        return self._path.relative_to(self._workspace).as_posix()

    @property
    def trace_complete(self) -> bool:
        """Return whether all attempted writes have completed."""

        return self._trace_complete

    def emit(self, event: RunEvent) -> None:
        """Write and flush one complete line or disable the recorder once."""

        if self._disabled:
            return
        if event.run_id != self.run_id:
            self._trace_complete = False
            self._disabled = True
            self._close_stream()
            raise EventSinkError(
                "TRACE_WRITE_ERROR",
                "trace event run_id did not match the locally selected run",
            )
        safe_event = event.sanitized(sensitive_values=self._sensitive_values)
        encoded = safe_event.to_json().encode("utf-8") + b"\n"
        try:
            if self._stream is None:
                raise OSError
            self._stream.write(encoded)
            self._stream.flush()
        except OSError:
            self._trace_complete = False
            self._disabled = True
            self._close_stream()
            raise EventSinkError(
                "TRACE_WRITE_ERROR",
                "trace write failed; the run continues with an incomplete trace",
            ) from None

    def close(self) -> None:
        """Close the trace without changing its completeness state."""

        self._close_stream()

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                self._trace_complete = False

    def __enter__(self) -> TraceRecorder:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def read_trace(workspace: Path, run_id: str) -> TraceReadResult:
    """Read one workspace-owned trace without executing any recorded content."""

    workspace_root = workspace.resolve(strict=True)
    path = _trace_path(workspace_root, validate_run_id(run_id))
    if not path.is_file():
        raise TracePathError("TRACE_NOT_FOUND", "trace does not exist for the requested run_id")

    events: list[RunEvent] = []
    issues: list[TraceIssue] = []
    previous_sequence = 0
    try:
        with path.open("rb") as stream:
            for line_number, raw_line, complete_line in _bounded_lines(stream):
                if len(raw_line) > MAX_TRACE_LINE_BYTES:
                    issues.append(
                        TraceIssue(
                            "EVENT_TOO_LARGE",
                            line_number,
                            "trace event exceeded the hard line limit and was skipped",
                        )
                    )
                    continue
                if not complete_line:
                    issues.append(
                        TraceIssue(
                            "TRUNCATED_TAIL",
                            line_number,
                            "trace ended with a non-LF tail line",
                        )
                    )
                event, issue = _decode_event(raw_line, run_id=run_id, line_number=line_number)
                if issue is not None:
                    issues.append(issue)
                    continue
                assert event is not None
                if event.sequence <= previous_sequence:
                    issues.append(
                        TraceIssue(
                            "INVALID_SEQUENCE",
                            line_number,
                            "event sequence was not strictly increasing",
                        )
                    )
                previous_sequence = max(previous_sequence, event.sequence)
                events.append(event)
    except OSError:
        raise TracePathError(
            "TRACE_READ_ERROR",
            "trace could not be read safely from the workspace",
        ) from None

    events.sort(key=lambda item: item.sequence)
    termination = next(
        (event for event in reversed(events) if event.event_type is EventType.TERMINATION),
        None,
    )
    if termination is None:
        issues.append(
            TraceIssue(
                "MISSING_TERMINATION",
                None,
                "trace has no termination event and is incomplete",
            )
        )
    terminated_complete = bool(
        termination is not None and termination.payload.get("trace_complete", True)
    )
    trace_complete = terminated_complete and not issues
    return TraceReadResult(
        run_id=run_id,
        trace_path=path.relative_to(workspace_root).as_posix(),
        events=tuple(events),
        issues=tuple(issues),
        trace_complete=trace_complete,
    )


def list_traces(workspace: Path) -> tuple[TraceSummary, ...]:
    """Return safe summaries for all strict run directories in sorted order."""

    workspace_root = workspace.resolve(strict=True)
    runs = workspace_root / TRACE_RELATIVE_ROOT
    _ensure_within_workspace(workspace_root, runs)
    if not runs.exists():
        return ()
    if not runs.is_dir():
        raise TracePathError("TRACE_PATH_UNSAFE", "trace root is not a directory")

    summaries: list[TraceSummary] = []
    try:
        entries = sorted(runs.iterdir(), key=lambda item: item.name)
    except OSError:
        raise TracePathError("TRACE_READ_ERROR", "trace root could not be listed safely") from None
    for entry in entries:
        if not entry.is_dir() or RUN_ID_PATTERN.fullmatch(entry.name) is None:
            continue
        try:
            result = read_trace(workspace_root, entry.name)
        except TracePathError:
            summaries.append(TraceSummary(entry.name, "unknown", "incomplete", 0, False))
            continue
        summaries.append(_trace_summary(result))
    return tuple(summaries)


def final_trace_report(result: TraceReadResult) -> str:
    """Build the run-statistics summary that complements the rendered DONE block.

    Status, changed files, verification, and trace identity are already shown by the
    terminal termination rendering, so this line carries only the per-run counters.
    """

    termination = next(
        (event for event in reversed(result.events) if event.event_type is EventType.TERMINATION),
        None,
    )
    if termination is None:
        return (
            f"run_id={result.run_id} status=incomplete events={len(result.events)} "
            f"trace_complete=false"
        )
    payload = termination.payload
    return (
        f"events={len(result.events)} model_calls={payload.get('model_calls', 0)} "
        f"tool_calls={payload.get('tool_calls', 0)} "
        f"tool_errors={payload.get('tool_errors', 0)} "
        f"api_attempts={payload.get('api_attempts', 0)} "
        f"api_retries={payload.get('api_retries', 0)} "
        f"context_compactions={payload.get('context_compactions', 0)} "
        f"input_tokens={payload.get('input_tokens', 0)} "
        f"output_tokens={payload.get('output_tokens', 0)} "
        f"elapsed_seconds={_format_elapsed_seconds(payload.get('elapsed_seconds'))} "
        f"trace_complete={str(result.trace_complete).lower()}"
    )


def _format_elapsed_seconds(value: object) -> str:
    """Format elapsed seconds from an untrusted payload without raising.

    A trace file is external input: ``read_trace`` preserves whatever JSON each
    payload carried, so a missing or non-numeric value must degrade to a safe
    token rather than crash ``trace show``.
    """

    if type(value) in {int, float}:
        return f"{value:.3f}"
    return "unknown"


def _trace_summary(result: TraceReadResult) -> TraceSummary:
    started = result.events[0].timestamp if result.events else "unknown"
    termination = next(
        (event for event in reversed(result.events) if event.event_type is EventType.TERMINATION),
        None,
    )
    if termination is None:
        status = "incomplete"
    else:
        completion = termination.payload.get("completion_status")
        status = str(
            completion
            if completion not in {None, "none"}
            else termination.payload.get("termination_reason") or "incomplete"
        )
    return TraceSummary(
        run_id=result.run_id,
        started_at=started,
        status=status,
        event_count=len(result.events),
        trace_complete=result.trace_complete,
    )


def _create_run_directory(workspace: Path, run_id: str) -> Path:
    current = workspace
    for part in (*TRACE_RELATIVE_ROOT.parts, run_id):
        candidate = current / part
        if candidate.exists():
            _ensure_within_workspace(workspace, candidate)
            if not candidate.is_dir():
                raise TracePathError(
                    "TRACE_PATH_UNSAFE",
                    "trace path component is not a directory",
                )
        else:
            try:
                candidate.mkdir(mode=0o700)
            except OSError:
                raise TracePathError(
                    "TRACE_PATH_UNAVAILABLE",
                    "trace directory could not be created safely inside the workspace",
                ) from None
            _ensure_within_workspace(workspace, candidate)
        current = candidate
    return current


def _trace_path(workspace: Path, run_id: str) -> Path:
    path = workspace / TRACE_RELATIVE_ROOT / run_id / TRACE_FILENAME
    _ensure_within_workspace(workspace, path)
    return path


def _ensure_within_workspace(workspace: Path, candidate: Path) -> None:
    try:
        candidate.resolve(strict=False).relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError):
        raise TracePathError(
            "TRACE_PATH_UNSAFE",
            "trace path resolves outside the selected workspace",
        ) from None


def _bounded_lines(stream: BinaryIO) -> Iterable[tuple[int, bytes, bool]]:
    line_number = 0
    while True:
        raw = stream.readline(MAX_TRACE_LINE_BYTES + 1)
        if not raw:
            return
        line_number += 1
        too_large = len(raw) > MAX_TRACE_LINE_BYTES and not raw.endswith(b"\n")
        if too_large:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(MAX_TRACE_LINE_BYTES + 1)
            yield line_number, b"x" * (MAX_TRACE_LINE_BYTES + 1), True
            continue
        complete = raw.endswith(b"\n")
        yield line_number, raw.removesuffix(b"\n"), complete


def _decode_event(
    raw: bytes,
    *,
    run_id: str,
    line_number: int,
) -> tuple[RunEvent | None, TraceIssue | None]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, TraceIssue(
            "MALFORMED_JSONL",
            line_number,
            "trace line is not a complete UTF-8 JSON object",
        )
    if not isinstance(decoded, dict):
        return None, TraceIssue(
            "MALFORMED_EVENT",
            line_number,
            "trace line is not a JSON object",
        )
    if decoded.get("schema_version") != SCHEMA_VERSION:
        return None, TraceIssue(
            "UNKNOWN_SCHEMA",
            line_number,
            "trace event uses an unsupported schema_version",
        )
    event_run_id = decoded.get("run_id")
    sequence = decoded.get("sequence")
    step = decoded.get("step")
    timestamp = decoded.get("timestamp")
    event_type_value = decoded.get("event_type")
    payload = decoded.get("payload")
    if (
        event_run_id != run_id
        or type(sequence) is not int
        or sequence < 1
        or type(step) is not int
        or step < 0
        or not isinstance(timestamp, str)
        or not _valid_timestamp(timestamp)
        or not isinstance(event_type_value, str)
        or not isinstance(payload, dict)
    ):
        return None, TraceIssue(
            "MALFORMED_EVENT",
            line_number,
            "trace event is missing required typed fields",
        )
    try:
        event_type = EventType(event_type_value)
    except ValueError:
        return None, TraceIssue(
            "UNKNOWN_EVENT_TYPE",
            line_number,
            "trace event_type is not supported",
        )
    return (
        RunEvent(
            run_id=run_id,
            sequence=sequence,
            step=step,
            timestamp=timestamp,
            event_type=event_type,
            payload=sanitize_payload(payload),
        ),
        None,
    )


def _valid_timestamp(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)
