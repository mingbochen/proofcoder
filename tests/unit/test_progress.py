"""Whole-batch no-progress fingerprint tests."""

from __future__ import annotations

from proofcoder.progress import NoProgressTracker, batch_fingerprint
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolResult


def _call(call_id: str, arguments: str = '{"path":"."}') -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name="probe", arguments=arguments),
    )


def test_fingerprint_excludes_ids_durations_and_random_audit_paths() -> None:
    left = ToolResult.success(
        {"value": 1, "audit_path": ".proofcoder/runtime/commands/random-a.json"},
        duration_ms=10,
    )
    right = ToolResult.success(
        {"audit_path": ".proofcoder/runtime/commands/random-b.json", "value": 1},
        duration_ms=999,
    )

    assert batch_fingerprint((_call("one"),), (left,)) == batch_fingerprint(
        (_call("two", '{ "path" : "." }'),),
        (right,),
    )


def test_second_identical_batch_warns_and_third_terminates() -> None:
    tracker = NoProgressTracker()
    result = ToolResult.success({"value": 1})

    first = tracker.observe((_call("1"),), (result,), modified_workspace=False)
    second = tracker.observe((_call("2"),), (result,), modified_workspace=False)
    third = tracker.observe((_call("3"),), (result,), modified_workspace=False)

    assert (first.repeat_count, first.warn, first.terminate) == (1, False, False)
    assert (second.repeat_count, second.warn, second.terminate) == (2, True, False)
    assert (third.repeat_count, third.warn, third.terminate) == (3, False, True)


def test_different_batch_and_successful_modification_reset_sequence() -> None:
    tracker = NoProgressTracker()
    result = ToolResult.success({"value": 1})
    tracker.observe((_call("1"),), (result,), modified_workspace=False)
    tracker.observe((_call("2"),), (result,), modified_workspace=False)

    changed = tracker.observe(
        (_call("3", '{"path":"other"}'),),
        (result,),
        modified_workspace=False,
    )
    modified = tracker.observe((_call("4"),), (result,), modified_workspace=True)
    after = tracker.observe((_call("5"),), (result,), modified_workspace=False)

    assert changed.repeat_count == 1
    assert modified.repeat_count == 0
    assert after.repeat_count == 1
