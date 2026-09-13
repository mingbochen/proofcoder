"""Rollback as one recorded local operation.

Applying a checkpoint is a user action against a finished run, not part of that
run. Its own trace therefore gets a fresh run identifier, and the event payload
names the run it undoes through ``target_run_id``. Appending to the target's trace
is not an option: that file was closed with its termination event, and a trace whose
termination is not last is exactly what the reader treats as damaged.

The command line and the local browser both go through here so the two entry points
record the same evidence for the same action instead of maintaining two descriptions
of it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from proofcoder.checkpoint import (
    RollbackPlan,
    RollbackResult,
    apply_rollback,
    plan_rollback,
    rollback_event_payload,
    tool_written_paths,
)
from proofcoder.events import (
    CompositeSink,
    EventEmitter,
    EventSink,
    EventType,
    new_run_id,
)
from proofcoder.protocol import TerminationReason
from proofcoder.trace import TracePathError, TraceRecorder


@dataclass(frozen=True, slots=True)
class RecordedRollback:
    """One applied rollback together with the trace that records it."""

    run_id: str
    target_run_id: str
    result: RollbackResult
    trace_path: str
    trace_complete: bool

    @property
    def complete(self) -> bool:
        """Return whether every planned action succeeded and was recorded."""

        return self.result.complete and self.trace_complete


def build_rollback_plan(workspace: Path, target_run_id: str) -> RollbackPlan:
    """Return what a rollback of one run would change, without changing anything.

    The plan labels each path with the source the trace can prove, so a preview
    distinguishes what ProofCoder's own tools wrote from what something else did.
    """

    return plan_rollback(
        workspace,
        target_run_id,
        tool_written_paths=tool_written_paths(workspace, target_run_id),
    )


def perform_rollback(
    workspace: Path,
    plan: RollbackPlan,
    *,
    additional_sinks: Sequence[EventSink] = (),
    run_id_factory: Callable[[], str] = new_run_id,
    sensitive_values: tuple[str, ...] = (),
) -> RecordedRollback:
    """Apply one plan and record the outcome as its own workspace trace.

    The rollback runs even if the trace cannot be opened: losing the record is worse
    than losing the recovery, but it is not a reason to leave the workspace in the
    state a run left it. A trace failure is reported through ``trace_complete``.
    """

    run_id = run_id_factory()
    recorder: TraceRecorder | None = None
    try:
        recorder = TraceRecorder(workspace, run_id, sensitive_values=sensitive_values)
    except TracePathError:
        recorder = None

    sinks: list[EventSink] = list(additional_sinks)
    if recorder is not None:
        sinks.append(recorder)
    emitter = EventEmitter(
        run_id=run_id,
        sink=CompositeSink(*sinks),
        sensitive_values=sensitive_values,
    )
    try:
        # Interruption is control flow, not a failure to convert: the recorder closes
        # and the exception propagates. The checkpoint survives and rollback is
        # idempotent, so re-running the command finishes what this one started.
        result = apply_rollback(workspace, plan)
        emitter.emit(EventType.ROLLBACK, step=0, payload=rollback_event_payload(result))
        emitter.emit(
            EventType.TERMINATION,
            step=0,
            payload=_termination_payload(
                emitter=emitter,
                result=result,
                trace_path="" if recorder is None else recorder.trace_path,
            ),
        )
        trace_complete = emitter.trace_complete and (
            recorder is not None and recorder.trace_complete
        )
        trace_path = "" if recorder is None else recorder.trace_path
    finally:
        if recorder is not None:
            recorder.close()

    return RecordedRollback(
        run_id=run_id,
        target_run_id=plan.run_id,
        result=result,
        trace_path=trace_path,
        trace_complete=trace_complete,
    )


def rollback_exit_code(recorded: RecordedRollback) -> int:
    """Map one recorded rollback onto its process exit code."""

    return 0 if recorded.result.complete else 1


def _termination_payload(
    *,
    emitter: EventEmitter,
    result: RollbackResult,
    trace_path: str,
) -> dict[str, object]:
    """Build a termination payload shaped like every other trace's closing event.

    A rollback has no model calls or tool calls, so those counters are zero rather
    than absent: the trace reader and the final report read the same keys for every
    recorded operation.
    """

    return {
        "api_attempts": 0,
        "api_retries": 0,
        "changed_files": list(result.restored + result.recreated + result.deleted),
        "completion_status": "none",
        "context_compactions": 0,
        "elapsed_seconds": 0.0,
        "event_count": emitter.event_count + 1,
        "input_tokens": 0,
        "model_calls": 0,
        "output_tokens": 0,
        "target_run_id": result.run_id,
        "termination_reason": TerminationReason.ROLLBACK.value,
        "tool_calls": 0,
        "tool_errors": 0,
        "trace_complete": emitter.trace_complete,
        "trace_path": trace_path,
        "verification": None,
        "warning_count": emitter.warning_count,
    }
