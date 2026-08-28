"""Unit tests for finish_task validation and local completion decisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofcoder.protocol import CompletionStatus, FunctionCall, ToolCall
from proofcoder.state import RunState
from proofcoder.tools.base import ToolResult
from proofcoder.tools.finish import (
    FinishTaskRequest,
    build_finish_outcome,
    create_finish_task_tool,
)
from proofcoder.tools.registry import ToolRegistry
from proofcoder.verification import VerificationTracker


def _dispatch(workspace: Path, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(create_finish_task_tool(workspace))
    return registry.dispatch(
        ToolCall(
            id="finish-1",
            function=FunctionCall(name="finish_task", arguments=json.dumps(arguments)),
        )
    )


def _request(
    *,
    changed_files: tuple[str, ...] = (),
    verification_command: tuple[str, ...] | None = None,
    blocked_reason: str | None = None,
) -> FinishTaskRequest:
    return FinishTaskRequest(
        summary="summary",
        changed_files=changed_files,
        verification_command=verification_command,
        limitations=("known limitation",),
        blocked_reason=blocked_reason,
    )


def _modification(path: str = "changed.py") -> ToolResult:
    return ToolResult.success({"path": path})


def _verification(*, exit_code: int = 0) -> ToolResult:
    return ToolResult.success(
        {
            "argv": ["python", "-m", "pytest", "-q"],
            "cwd": ".",
            "exit_code": exit_code,
            "timed_out": False,
            "command_kind": "test",
        }
    )


def test_finish_schema_defaults_and_tool_has_no_workspace_side_effect(tmp_path: Path) -> None:
    before = tuple(tmp_path.iterdir())

    result = _dispatch(tmp_path, {"summary": "done"})

    assert result.ok is True
    assert result.data == {
        "accepted": True,
        "message": "AgentLoop must determine completion from injected local evidence",
    }
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("forbidden", ["verified", "status", "exit_code", "unknown"])
def test_finish_rejects_unknown_and_local_status_fields(
    tmp_path: Path,
    forbidden: str,
) -> None:
    result = _dispatch(tmp_path, {"summary": "done", forbidden: True})

    assert result.error is not None
    assert result.error.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"summary": "   "},
        {"summary": "done", "changed_files": "file.py"},
        {"summary": "done", "changed_files": [""]},
        {"summary": "done", "verification_command": []},
        {"summary": "done", "verification_command": ["python", 1]},
        {"summary": "done", "limitations": [""]},
        {"summary": "done", "blocked_reason": " "},
    ],
)
def test_finish_rejects_invalid_nested_values(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    result = _dispatch(tmp_path, arguments)

    assert result.error is not None
    assert result.error.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../outside.py", "PATH_OUTSIDE_WORKSPACE"),
        ("/outside.py", "PATH_OUTSIDE_WORKSPACE"),
        (r"C:\outside.py", "PATH_OUTSIDE_WORKSPACE"),
        (".env", "SENSITIVE_PATH"),
        (".proofcoder/state.json", "SENSITIVE_PATH"),
    ],
)
def test_finish_rejects_unsafe_claimed_paths(tmp_path: Path, path: str, code: str) -> None:
    result = _dispatch(tmp_path, {"summary": "done", "changed_files": [path]})

    assert result.error is not None
    assert result.error.code == code


def test_local_statuses_cover_verified_unverified_no_changes_and_blocked() -> None:
    no_changes = VerificationTracker(RunState(original_task="task"))
    assert build_finish_outcome(_request(), no_changes).status is (
        CompletionStatus.COMPLETED_NO_CHANGES
    )

    unverified = VerificationTracker(RunState(original_task="task"))
    unverified.record_execution("create_file", _modification())
    assert build_finish_outcome(_request(changed_files=("changed.py",)), unverified).status is (
        CompletionStatus.COMPLETED_UNVERIFIED
    )

    verified = VerificationTracker(RunState(original_task="task"))
    verified.record_execution("create_file", _modification())
    verified.record_execution("run_command", _verification())
    assert (
        build_finish_outcome(
            _request(
                changed_files=("changed.py",),
                verification_command=("python", "-m", "pytest", "-q"),
            ),
            verified,
        ).status
        is CompletionStatus.COMPLETED_VERIFIED
    )

    assert (
        build_finish_outcome(
            _request(blocked_reason="external dependency unavailable"),
            no_changes,
        ).status
        is CompletionStatus.BLOCKED
    )


def test_verification_then_modification_and_failed_verification_are_unverified() -> None:
    reordered = VerificationTracker(RunState(original_task="task"))
    reordered.record_execution("run_command", _verification())
    reordered.record_execution("create_file", _modification())

    failed = VerificationTracker(RunState(original_task="task"))
    failed.record_execution("create_file", _modification())
    failed.record_execution("run_command", _verification(exit_code=1))

    changed_again = VerificationTracker(RunState(original_task="task"))
    changed_again.record_execution("create_file", _modification())
    changed_again.record_execution("run_command", _verification())
    changed_again.record_execution("replace_in_file", _modification())

    for tracker in (reordered, failed, changed_again):
        outcome = build_finish_outcome(_request(changed_files=("changed.py",)), tracker)
        assert outcome.status is CompletionStatus.COMPLETED_UNVERIFIED


def test_model_claim_conflicts_warn_and_local_evidence_wins() -> None:
    tracker = VerificationTracker(RunState(original_task="task"))
    tracker.record_execution("create_file", _modification("actual.py"))

    outcome = build_finish_outcome(
        _request(
            changed_files=("claimed.py",),
            verification_command=("python", "-m", "pytest", "-q"),
        ),
        tracker,
    )

    assert outcome.status is CompletionStatus.COMPLETED_UNVERIFIED
    assert outcome.result.data is not None
    assert outcome.result.data["changed_files"] == ["actual.py"]
    assert len(outcome.result.meta.warnings) == 2
    assert "MODEL_CHANGED_FILES_MISMATCH" in outcome.final_report
    assert "MODEL_VERIFICATION_UNCONFIRMED" in outcome.final_report


def test_final_report_lists_every_real_command_exit_code() -> None:
    tracker = VerificationTracker(RunState(original_task="task"))
    tracker.record_execution("create_file", _modification())
    tracker.record_execution("run_command", _verification(exit_code=1))
    tracker.record_execution("replace_in_file", _modification())
    tracker.record_execution("run_command", _verification(exit_code=0))

    outcome = build_finish_outcome(_request(changed_files=("changed.py",)), tracker)

    assert "exit_code=1" in outcome.final_report
    assert "exit_code=0" in outcome.final_report
    assert outcome.result.data is not None
    observations = outcome.result.data["command_observations"]
    assert isinstance(observations, list)
    assert [item["exit_code"] for item in observations] == [1, 0]
