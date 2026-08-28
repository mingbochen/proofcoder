"""Stable whole-batch fingerprints and consecutive no-progress tracking."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from proofcoder.protocol import ToolCall
from proofcoder.tools.base import ToolResult

NO_PROGRESS_WARNING_REPEAT = 2
NO_PROGRESS_TERMINATION_REPEAT = 3
_UNSTABLE_KEYS = {
    "audit_path",
    "call_id",
    "duration_ms",
    "elapsed",
    "elapsed_seconds",
    "request_id",
    "timestamp",
    "timestamps",
    "tool_call_id",
}


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    """Result of comparing one completed batch with its predecessor."""

    repeat_count: int
    warn: bool
    terminate: bool


class NoProgressTracker:
    """Detect three identical consecutive semantic tool batches."""

    def __init__(self) -> None:
        self._fingerprint: str | None = None
        self._repeat_count = 0

    def observe(
        self,
        calls: tuple[ToolCall, ...],
        results: tuple[ToolResult, ...],
        *,
        modified_workspace: bool,
    ) -> ProgressObservation:
        """Record a completed batch, resetting after successful modification."""

        if modified_workspace:
            self.reset()
            return ProgressObservation(0, False, False)
        fingerprint = batch_fingerprint(calls, results)
        if fingerprint == self._fingerprint:
            self._repeat_count += 1
        else:
            self._fingerprint = fingerprint
            self._repeat_count = 1
        return ProgressObservation(
            repeat_count=self._repeat_count,
            warn=self._repeat_count == NO_PROGRESS_WARNING_REPEAT,
            terminate=self._repeat_count >= NO_PROGRESS_TERMINATION_REPEAT,
        )

    def reset(self) -> None:
        """Forget the previous semantic batch."""

        self._fingerprint = None
        self._repeat_count = 0


def batch_fingerprint(
    calls: tuple[ToolCall, ...],
    results: tuple[ToolResult, ...],
) -> str:
    """Hash names, normalized arguments, and stable semantic results."""

    if len(calls) != len(results):
        raise ValueError("calls and results must have equal lengths")
    batch = [
        {
            "arguments": _normalized_arguments(call.function.arguments),
            "name": call.function.name,
            "result": _semantic_result(result),
        }
        for call, result in zip(calls, results, strict=True)
    ]
    encoded = json.dumps(
        batch,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_arguments(arguments: str) -> object:
    try:
        return _stable_value(json.loads(arguments))
    except json.JSONDecodeError:
        return arguments.strip()


def _semantic_result(result: ToolResult) -> dict[str, object]:
    error = result.error
    return {
        "data": _stable_value(result.data),
        "error": (None if error is None else {"code": error.code, "retryable": error.retryable}),
        "ok": result.ok,
        "truncated": result.meta.truncated,
        "warnings": list(result.meta.warnings),
    }


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_value(item)
            for key, item in sorted(value.items())
            if key not in _UNSTABLE_KEYS
        }
    if isinstance(value, list):
        return [_stable_value(item) for item in value]
    return value
