"""Project-owned structured events, safe summaries, and synchronous sinks."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from proofcoder.safety.secrets import (
    is_safe_token_statistic,
    is_sensitive_environment_name,
    redact_text,
)
from proofcoder.tools.base import ToolResult

SCHEMA_VERSION = 1
MAX_EVENT_JSON_BYTES = 64 * 1024
MAX_EVENT_STRING_BYTES = 8 * 1024
MAX_EVENT_ARRAY_ITEMS = 64
MAX_EVENT_MAPPING_ITEMS = 64
MAX_EVENT_DEPTH = 6
MAX_DIFF_PREVIEW_BYTES = 8 * 1024

_DROPPED_PAYLOAD_KEYS = frozenset(
    {
        "authorization",
        "content",
        "env",
        "environment",
        "headers",
        "new_text",
        "old_text",
        "raw_request",
        "raw_response",
        "reasoning_content",
        "request_body",
        "response_body",
        "stderr",
        "stdout",
        "system_prompt",
        "traceback",
    }
)
_CONTENT_ARGUMENT_KEYS = frozenset({"content", "old_text", "new_text"})


def new_run_id() -> str:
    """Return a locally generated opaque run identifier."""

    return uuid.uuid4().hex


class EventType(StrEnum):
    """Stable event categories emitted at the point each action occurs."""

    TASK = "task"
    MODEL = "model"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DIFF = "diff"
    VERIFICATION = "verification"
    WARNING = "warning"
    COMPLETION = "completion"
    TERMINATION = "termination"


class EventSink(Protocol):
    """Receive one already ordered event synchronously."""

    def emit(self, event: RunEvent) -> None: ...


class EventSinkError(Exception):
    """A safe sink failure that may be converted into one warning event."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One immutable, schema-versioned local run event."""

    run_id: str
    sequence: int
    step: int
    timestamp: str
    event_type: EventType
    payload: dict[str, object]
    schema_version: int = SCHEMA_VERSION

    def sanitized(self, *, sensitive_values: tuple[str, ...] = ()) -> RunEvent:
        """Return an equivalent event with payload redaction and hard bounds applied."""

        payload = sanitize_payload(self.payload, sensitive_values=sensitive_values)
        return RunEvent(
            run_id=self.run_id,
            sequence=self.sequence,
            step=self.step,
            timestamp=self.timestamp,
            event_type=self.event_type,
            payload=payload,
            schema_version=self.schema_version,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible event mapping."""

        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "step": self.step,
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Serialize one complete bounded JSON object deterministically."""

        payload = self.to_dict()
        encoded = _canonical_json(payload)
        if len(encoded) <= MAX_EVENT_JSON_BYTES:
            return encoded.decode("utf-8")
        fallback = dict(payload)
        fallback["payload"] = {
            "original_json_bytes": len(encoded),
            "summary": "event payload exceeded the hard JSON limit",
            "truncated": True,
        }
        return _canonical_json(fallback).decode("utf-8")


class NoOpSink:
    """Discard events for non-observable library callers."""

    def emit(self, event: RunEvent) -> None:
        """Accept one event without retaining it."""


@dataclass(slots=True)
class MemorySink:
    """Retain events in emission order for deterministic offline tests."""

    events: list[RunEvent] = field(default_factory=list)

    def emit(self, event: RunEvent) -> None:
        """Append one event exactly once per sink call."""

        self.events.append(event)


class CompositeSink:
    """Fan out events while allowing healthy sinks to survive one sink failure."""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = sinks

    def emit(self, event: RunEvent) -> None:
        """Deliver to every sink, then report the first safe failure."""

        first_error: EventSinkError | None = None
        for sink in self._sinks:
            try:
                sink.emit(event)
            except EventSinkError as error:
                if first_error is None:
                    first_error = error
            except Exception:
                if first_error is None:
                    first_error = EventSinkError(
                        "EVENT_SINK_ERROR",
                        "an event sink failed without exposing implementation details",
                    )
        if first_error is not None:
            raise first_error


class EventEmitter:
    """Allocate local sequence numbers and timestamps before dispatching events."""

    def __init__(
        self,
        *,
        run_id: str,
        sink: EventSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sensitive_values: tuple[str, ...] = (),
    ) -> None:
        self.run_id = run_id
        self._sink = sink
        self._clock = clock
        self._sensitive_values = sensitive_values
        self._sequence = 0
        self._warning_count = 0
        self._sink_error_codes: list[str] = []
        self._handling_sink_error = False

    @property
    def event_count(self) -> int:
        """Return the number of locally allocated events."""

        return self._sequence

    @property
    def sink_error_codes(self) -> tuple[str, ...]:
        """Return unique safe sink-error codes in occurrence order."""

        return tuple(self._sink_error_codes)

    @property
    def warning_count(self) -> int:
        """Return the number of allocated warning events."""

        return self._warning_count

    @property
    def trace_complete(self) -> bool:
        """Return whether no trace/event sink failure was observed."""

        return not self._sink_error_codes

    def emit(
        self,
        event_type: EventType,
        *,
        step: int,
        payload: Mapping[str, object],
    ) -> RunEvent:
        """Allocate, sanitize, and synchronously deliver one event."""

        event = self._new_event(event_type, step=step, payload=payload)
        try:
            self._sink.emit(event)
        except EventSinkError as error:
            self._record_sink_error(error, step=step)
        except Exception:
            self._record_sink_error(
                EventSinkError(
                    "EVENT_SINK_ERROR",
                    "an event sink failed without exposing implementation details",
                ),
                step=step,
            )
        return event

    def _new_event(
        self,
        event_type: EventType,
        *,
        step: int,
        payload: Mapping[str, object],
    ) -> RunEvent:
        self._sequence += 1
        if event_type is EventType.WARNING:
            self._warning_count += 1
        return RunEvent(
            run_id=self.run_id,
            sequence=self._sequence,
            step=max(0, step),
            timestamp=_rfc3339(self._clock()),
            event_type=event_type,
            payload=sanitize_payload(payload, sensitive_values=self._sensitive_values),
        )

    def _record_sink_error(self, error: EventSinkError, *, step: int) -> None:
        if error.code in self._sink_error_codes or self._handling_sink_error:
            return
        self._sink_error_codes.append(error.code)
        self._handling_sink_error = True
        try:
            warning = self._new_event(
                EventType.WARNING,
                step=step,
                payload={
                    "code": error.code,
                    "message": str(error),
                    "trace_complete": False,
                },
            )
            with suppress(Exception):
                self._sink.emit(warning)
        finally:
            self._handling_sink_error = False


class TerminalSink:
    """Render safe structured events as deterministic terminal lines."""

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._seen: set[tuple[str, int]] = set()

    def emit(self, event: RunEvent) -> None:
        """Render one event once without accessing message history."""

        key = (event.run_id, event.sequence)
        if key in self._seen:
            return
        self._seen.add(key)
        line = render_terminal_event(event)
        if line is not None:
            self._write(line)


def render_terminal_event(event: RunEvent) -> str | None:
    """Return the deterministic single-event terminal rendering."""

    payload = event.payload
    if event.event_type is EventType.TASK:
        return f"TASK: {payload.get('task', '')}"
    if event.event_type is EventType.MODEL:
        return f"MODEL: {payload.get('text') or '<no visible text>'}"
    if event.event_type is EventType.TOOL_CALL:
        arguments = json.dumps(
            payload.get("arguments", {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return (
            f"TOOL: {payload.get('tool_name', 'unknown')} "
            f"(id={payload.get('tool_call_id', 'unknown')}) args={arguments}"
        )
    if event.event_type is EventType.TOOL_RESULT:
        return (
            f"RESULT: id={payload.get('tool_call_id', 'unknown')} "
            f"success={str(bool(payload.get('success'))).lower()} "
            f"error_code={payload.get('error_code')} duration_ms={payload.get('duration_ms', 0)} "
            f"exit_code={payload.get('exit_code')} "
            f"truncated={str(bool(payload.get('truncated'))).lower()}"
        )
    if event.event_type is EventType.DIFF:
        stats = json.dumps(
            payload.get("stats", {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        preview = payload.get("preview", "")
        return f"DIFF: path={payload.get('path')} stats={stats}\n{preview}".rstrip()
    if event.event_type is EventType.VERIFICATION:
        argv = json.dumps(payload.get("argv", []), ensure_ascii=False, separators=(",", ":"))
        return (
            f"VERIFY: argv={argv} cwd={payload.get('cwd')} "
            f"exit_code={payload.get('exit_code')} "
            f"accepted={str(bool(payload.get('accepted'))).lower()}"
        )
    if event.event_type is EventType.WARNING:
        message = payload.get("message")
        detail = "" if not message else f" ({message})"
        return f"WARN: {payload.get('code', 'WARNING')}{detail}"
    if event.event_type is EventType.COMPLETION:
        return None
    if event.event_type is EventType.TERMINATION:
        changed_files = json.dumps(
            payload.get("changed_files", []), ensure_ascii=False, separators=(",", ":")
        )
        verification = payload.get("verification")
        verification_argv = None
        verification_cwd = None
        verification_exit_code = None
        if isinstance(verification, Mapping):
            verification_argv = verification.get("argv")
            verification_cwd = verification.get("cwd")
            verification_exit_code = verification.get("exit_code")
        rendered_argv = (
            "null"
            if verification_argv is None
            else json.dumps(verification_argv, ensure_ascii=False, separators=(",", ":"))
        )
        return (
            f"DONE: termination={payload.get('termination_reason')} "
            f"completion={payload.get('completion_status')} changed_files={changed_files} "
            f"verification_argv={rendered_argv} verification_cwd={verification_cwd} "
            f"verification_exit_code={verification_exit_code} "
            f"model_calls={payload.get('model_calls', 0)} "
            f"tool_calls={payload.get('tool_calls', 0)} "
            f"tool_errors={payload.get('tool_errors', 0)} "
            f"elapsed_seconds={float(payload.get('elapsed_seconds', 0.0)):.3f} "
            f"api_attempts={payload.get('api_attempts', 0)} "
            f"api_retries={payload.get('api_retries', 0)} "
            f"context_compactions={payload.get('context_compactions', 0)} "
            f"input_tokens={payload.get('input_tokens', 0)} "
            f"output_tokens={payload.get('output_tokens', 0)} "
            f"run_id={event.run_id} trace_path={payload.get('trace_path')} "
            f"trace_complete={str(bool(payload.get('trace_complete'))).lower()}"
        )
    return None


def summarize_tool_arguments(
    tool_name: str,
    raw_arguments: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a bounded argument summary that omits write bodies."""

    raw_bytes = raw_arguments.encode("utf-8")
    try:
        decoded = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {
            "arguments_bytes": len(raw_bytes),
            "arguments_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "valid_json": False,
        }
    if not isinstance(decoded, dict):
        return {
            "arguments_bytes": len(raw_bytes),
            "arguments_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "valid_json": True,
            "json_type": type(decoded).__name__,
        }

    if tool_name in {"create_file", "replace_in_file"}:
        summary: dict[str, object] = {}
        for key, value in decoded.items():
            if key in _CONTENT_ARGUMENT_KEYS and isinstance(value, str):
                encoded = value.encode("utf-8")
                summary[f"{key}_bytes"] = len(encoded)
                summary[f"{key}_sha256"] = hashlib.sha256(encoded).hexdigest()
            elif key not in _CONTENT_ARGUMENT_KEYS:
                summary[key] = value
        return sanitize_payload(summary, sensitive_values=sensitive_values)

    if tool_name in {
        "list_files",
        "search_text",
        "read_file",
        "run_command",
        "finish_task",
    }:
        return sanitize_payload(decoded, sensitive_values=sensitive_values)
    return {
        "argument_keys": sorted(str(key) for key in decoded)[:MAX_EVENT_ARRAY_ITEMS],
        "arguments_bytes": len(raw_bytes),
        "arguments_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "valid_json": True,
    }


def summarize_tool_result(
    tool_name: str,
    tool_call_id: str,
    result: ToolResult,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a tool-specific safe result summary without raw file or command output."""

    payload: dict[str, object] = {
        "duration_ms": result.meta.duration_ms,
        "error_code": None if result.error is None else result.error.code,
        "success": result.ok,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "truncated": result.meta.truncated,
    }
    if result.error is not None:
        payload["error_message"] = result.error.message
        payload["retryable"] = result.error.retryable
    if result.meta.warnings:
        payload["warnings"] = list(result.meta.warnings)

    data = result.data or {}
    field_names: tuple[str, ...]
    if tool_name == "list_files":
        field_names = (
            "queried_path",
            "returned_count",
            "total_matched_count",
            "truncated_count",
        )
    elif tool_name == "search_text":
        field_names = ("query", "queried_path", "returned_count", "more_matches_available")
    elif tool_name == "read_file":
        field_names = (
            "path",
            "total_lines",
            "actual_start_line",
            "actual_end_line",
            "returned_line_count",
            "returned_bytes",
            "encoding",
            "newline_style",
        )
    elif tool_name in {"create_file", "replace_in_file"}:
        field_names = ("path", "bytes_written", "encoding", "replacements", "diff_stats")
    elif tool_name == "run_command":
        field_names = (
            "argv",
            "cwd",
            "command_kind",
            "exit_code",
            "stdout_bytes",
            "stderr_bytes",
            "stdout_truncated",
            "stderr_truncated",
            "timed_out",
            "audit_truncated",
        )
    elif tool_name == "finish_task":
        field_names = (
            "completion_status",
            "changed_files",
            "verification",
            "limitations",
            "blocked_reason",
        )
    else:
        field_names = ()
    for name in field_names:
        if name in data:
            payload[name] = data[name]
    if "exit_code" in data:
        payload["exit_code"] = data["exit_code"]
    return sanitize_payload(payload, sensitive_values=sensitive_values)


def diff_event_payload(
    tool_name: str,
    tool_call_id: str,
    result: ToolResult,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object] | None:
    """Return a bounded diff event for one successful local modification."""

    if tool_name not in {"create_file", "replace_in_file"} or not result.ok:
        return None
    data = result.data or {}
    path = data.get("path")
    stats = data.get("diff_stats")
    preview = data.get("diff")
    if not isinstance(path, str) or not isinstance(stats, Mapping):
        return None
    preview_text = preview if isinstance(preview, str) else ""
    bounded_preview, preview_truncated, original_bytes = bound_text(
        redact_text(preview_text, sensitive_values=sensitive_values),
        MAX_DIFF_PREVIEW_BYTES,
    )
    return sanitize_payload(
        {
            "path": path,
            "preview": bounded_preview,
            "preview_bytes": original_bytes,
            "preview_truncated": preview_truncated or result.meta.truncated,
            "stats": dict(stats),
            "tool_call_id": tool_call_id,
        },
        sensitive_values=sensitive_values,
    )


def verification_event_payload(
    tool_call_id: str,
    result: ToolResult,
    *,
    accepted: bool,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object] | None:
    """Return real command evidence when run_command reached execution."""

    data = result.data
    if data is None or not isinstance(data.get("argv"), list):
        return None
    return sanitize_payload(
        {
            "accepted": accepted,
            "argv": data.get("argv"),
            "command_kind": data.get("command_kind"),
            "cwd": data.get("cwd"),
            "exit_code": data.get("exit_code"),
            "timed_out": data.get("timed_out"),
            "tool_call_id": tool_call_id,
        },
        sensitive_values=sensitive_values,
    )


def sanitize_payload(
    payload: Mapping[str, object],
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object]:
    """Redact and deterministically bound arbitrary event payload data."""

    counters = {"arrays": 0, "mappings": 0, "strings": 0}
    sanitized = _sanitize_value(
        dict(payload),
        sensitive_values=sensitive_values,
        depth=0,
        counters=counters,
    )
    result = sanitized if isinstance(sanitized, dict) else {"value": sanitized}
    if any(counters.values()):
        result["truncated"] = True
        result["truncation"] = {key: value for key, value in counters.items() if value}
    return result


def bound_text(text: str, maximum_bytes: int) -> tuple[str, bool, int]:
    """Return a UTF-8 head/tail view with deterministic byte statistics."""

    encoded = text.encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= maximum_bytes:
        return text, False, original_bytes
    marker = b"\n... truncated ...\n"
    available = max(0, maximum_bytes - len(marker))
    head_bytes = available // 2
    tail_bytes = available - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return f"{head}{marker.decode()}{tail}", True, original_bytes


def _sanitize_value(
    value: object,
    *,
    sensitive_values: tuple[str, ...],
    depth: int,
    counters: dict[str, int],
) -> object:
    if depth >= MAX_EVENT_DEPTH:
        counters["mappings"] += 1
        return "[truncated-depth]"
    if isinstance(value, str):
        redacted = redact_text(value, sensitive_values=sensitive_values)
        bounded, truncated, _ = bound_text(redacted, MAX_EVENT_STRING_BYTES)
        if truncated:
            counters["strings"] += 1
        return bounded
    if type(value) is float and not math.isfinite(value):
        return "[non-finite]"
    if value is None or type(value) in {bool, int, float}:
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        items = sorted(value.items(), key=lambda item: str(item[0]))
        if len(items) > MAX_EVENT_MAPPING_ITEMS:
            counters["mappings"] += 1
            items = items[:MAX_EVENT_MAPPING_ITEMS]
        for raw_key, item in items:
            key = str(raw_key)
            lowered = key.casefold()
            if lowered in _DROPPED_PAYLOAD_KEYS:
                continue
            if is_sensitive_environment_name(key) and not is_safe_token_statistic(key, item):
                continue
            result[key] = _sanitize_value(
                item,
                sensitive_values=sensitive_values,
                depth=depth + 1,
                counters=counters,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        if len(items) > MAX_EVENT_ARRAY_ITEMS:
            counters["arrays"] += 1
            items = items[:MAX_EVENT_ARRAY_ITEMS]
        return [
            _sanitize_value(
                item,
                sensitive_values=sensitive_values,
                depth=depth + 1,
                counters=counters,
            )
            for item in items
        ]
    return redact_text(str(value), sensitive_values=sensitive_values)


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
