"""Unit tests for local modification and verification evidence tracking."""

from __future__ import annotations

from proofcoder.state import RunState
from proofcoder.tools.base import ToolResult
from proofcoder.verification import VerificationTracker


def _modification(path: str = "src/example.py", *, ok: bool = True) -> ToolResult:
    if ok:
        return ToolResult.success({"path": path})
    return ToolResult.failure("MATCH_NOT_FOUND", "not changed", retryable=True)


def _command(
    *,
    exit_code: int | None = 0,
    timed_out: bool = False,
    command_kind: str = "test",
) -> ToolResult:
    data: dict[str, object] = {
        "argv": ["python", "-m", "pytest", "-q"],
        "cwd": ".",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "command_kind": command_kind,
    }
    if timed_out:
        return ToolResult.failure(
            "COMMAND_TIMEOUT",
            "timed out",
            retryable=True,
            data=data,
        )
    return ToolResult.success(data)


def _tracker() -> VerificationTracker:
    return VerificationTracker(RunState(original_task="task"))


def test_successful_verification_after_modification_is_valid() -> None:
    tracker = _tracker()

    modification_event = tracker.record_execution("create_file", _modification())
    verification_event = tracker.record_execution("run_command", _command())

    assert verification_event > modification_event
    assert tracker.state.changed_files == ("src/example.py",)
    assert tracker.valid_verification is not None
    assert tracker.valid_verification.event_sequence == verification_event


def test_modification_invalidates_earlier_successful_verification() -> None:
    tracker = _tracker()
    tracker.record_execution("create_file", _modification())
    tracker.record_execution("run_command", _command())

    tracker.record_execution("replace_in_file", _modification())

    assert tracker.valid_verification is None
    assert len(tracker.state.command_observations) == 1


def test_verification_before_modification_cannot_prove_later_change() -> None:
    tracker = _tracker()
    tracker.record_execution("run_command", _command())

    tracker.record_execution("create_file", _modification())

    assert tracker.valid_verification is None


def test_nonzero_and_timeout_commands_are_observed_but_not_verification() -> None:
    tracker = _tracker()
    tracker.record_execution("create_file", _modification())

    tracker.record_execution("run_command", _command(exit_code=2))
    tracker.record_execution("run_command", _command(exit_code=-1, timed_out=True))

    assert tracker.valid_verification is None
    assert [item.exit_code for item in tracker.state.command_observations] == [2, -1]
    assert not any(item.accepted_as_verification for item in tracker.state.command_observations)


def test_read_only_diagnostics_and_scripts_are_not_verification() -> None:
    tracker = _tracker()
    tracker.record_execution("create_file", _modification())

    for kind in ("git_read", "script"):
        tracker.record_execution("run_command", _command(command_kind=kind))
    tracker.record_execution("read_file", ToolResult.success({"path": "src/example.py"}))
    tracker.record_execution("search_text", ToolResult.success({"matches": []}))

    assert tracker.valid_verification is None


def test_failed_modification_is_not_registered_or_invalidating() -> None:
    tracker = _tracker()
    tracker.record_execution("create_file", _modification())
    tracker.record_execution("run_command", _command())

    tracker.record_execution("replace_in_file", _modification(ok=False))

    assert tracker.state.changed_files == ("src/example.py",)
    assert tracker.valid_verification is not None


def test_multiple_modifications_use_strict_events_and_actual_result_paths() -> None:
    tracker = _tracker()

    first = tracker.record_execution("create_file", _modification("first.py"))
    second = tracker.record_execution("replace_in_file", _modification("second.py"))
    third = tracker.record_execution("replace_in_file", _modification("first.py"))

    assert first < second < third
    assert tracker.state.changed_files == ("first.py", "second.py")
    assert tracker.state.last_modified_events == {"first.py": third, "second.py": second}
    assert tracker.state.last_modification_event == third


def test_untrusted_or_invalid_modification_result_path_is_ignored() -> None:
    tracker = _tracker()

    tracker.record_execution("create_file", _modification("../outside.py"))
    tracker.record_execution("create_file", ToolResult.success({"path": 7}))

    assert tracker.state.changed_files == ()
