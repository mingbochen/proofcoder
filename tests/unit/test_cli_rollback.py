"""Stage F.3: the command-line rollback entry and the operation trace it records."""

from __future__ import annotations

import builtins
import io
from pathlib import Path

import pytest
from rich.console import Console

import proofcoder.rollback as rollback_module
from proofcoder.checkpoint import (
    BLOBS_DIRECTORY,
    CHECKPOINT_RELATIVE_ROOT,
    create_checkpoint,
    load_checkpoint,
)
from proofcoder.cli import main
from proofcoder.events import EventType
from proofcoder.trace import list_traces, read_trace

TARGET_RUN = "a" * 32
# Written into a workspace .env so the tests can prove no rollback surface shows it.
SENSITIVE_SENTINEL = "never-print-this-value"


def _invoke(argv: list[str], **kwargs: object) -> tuple[int, str]:
    stream = io.StringIO()
    code = main(
        argv,
        environ={},
        console=Console(file=stream, force_terminal=False, color_system=None, width=200),
        **kwargs,  # type: ignore[arg-type]
    )
    return code, stream.getvalue()


def _workspace(root: Path) -> Path:
    (root / "pkg").mkdir()
    (root / "pkg" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "gone.txt").write_text("gone\n", encoding="utf-8")
    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}\n", encoding="utf-8")
    create_checkpoint(root, TARGET_RUN)
    return root


def _change(root: Path) -> None:
    (root / "pkg" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (root / "gone.txt").unlink()
    (root / "build").mkdir()
    (root / "build" / "out.o").write_text("object\n", encoding="utf-8")
    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}-rotated\n", encoding="utf-8")


def test_list_reports_stored_checkpoints(tmp_path: Path) -> None:
    _workspace(tmp_path)

    code, output = _invoke(["rollback", "list", "--workspace", str(tmp_path)])

    assert code == 0
    assert "run_id created_at entries stored_bytes readable" in output
    assert TARGET_RUN in output
    assert "true" in output


def test_show_previews_every_action_and_gap_without_writing(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    code, output = _invoke(["rollback", "show", "--workspace", str(tmp_path), TARGET_RUN])

    assert code == 0
    assert f"PLAN: target={TARGET_RUN}" in output
    assert "restore: pkg/app.py" in output
    assert "recreate: gone.txt" in output
    assert "delete: build/out.o" in output
    assert "remove dir: build" in output
    assert "not covered: .env (sensitive_changed)" in output
    # Showing a plan changes nothing.
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (tmp_path / "gone.txt").exists()
    assert SENSITIVE_SENTINEL not in output


def test_apply_restores_the_workspace_after_confirmation(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    code, output = _invoke(
        ["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN],
        confirm_rollback=lambda: True,
    )

    assert code == 0
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "gone.txt").read_text(encoding="utf-8") == "gone\n"
    assert not (tmp_path / "build").exists()
    assert f"ROLLBACK: target={TARGET_RUN} restored=1 recreated=1 deleted=1" in output
    assert "termination=rollback" in output
    # The sensitive path is reported and left exactly as the run left it.
    assert (tmp_path / ".env").read_text(encoding="utf-8").endswith("-rotated\n")
    assert SENSITIVE_SENTINEL not in output


def test_declining_changes_nothing_and_is_distinguishable(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    code, output = _invoke(
        ["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN],
        confirm_rollback=lambda: False,
    )

    assert code == 3
    assert "ROLLBACK declined" in output
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (tmp_path / "gone.txt").exists()
    assert list_traces(tmp_path) == ()


def test_the_plan_is_always_shown_before_the_prompt(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    seen: list[str] = []

    def confirm() -> bool:
        # By the time confirmation is asked, the full plan has already been printed.
        seen.append("asked")
        return False

    _, output = _invoke(
        ["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN],
        confirm_rollback=confirm,
    )

    assert seen == ["asked"]
    assert output.index("PLAN: target=") < output.index("ROLLBACK declined")


def test_yes_applies_without_asking(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    def refuse() -> bool:
        raise AssertionError("--yes must not consult the confirmation callback")

    code, _ = _invoke(
        ["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"],
        confirm_rollback=refuse,
    )

    assert code == 0
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_without_a_terminal_the_prompt_refuses_rather_than_assuming(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    # pytest captures stdin, so isatty() is false here exactly as in a pipeline.
    code, output = _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN])

    assert code == 3
    assert "standard input is not a terminal" in output
    assert "--yes" in output
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.parametrize(
    ("answer", "expected_code", "expected_content"),
    [("y", 0, "value = 1\n"), ("YES", 0, "value = 1\n"), ("", 3, "value = 2\n")],
)
def test_the_terminal_prompt_only_accepts_an_explicit_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
    expected_code: int,
    expected_content: str,
) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Terminal())
    monkeypatch.setattr(builtins, "input", lambda _prompt="": answer)
    code, _ = _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN])

    assert code == expected_code
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == expected_content


def test_a_second_apply_finds_nothing_to_undo(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"])

    code, output = _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"])

    assert code == 0
    assert "no changes to undo" in output
    # A no-op records no operation trace, so only the first rollback is listed.
    assert len(list_traces(tmp_path)) == 1


def test_partial_failure_is_reported_and_exits_nonzero(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    entry = next(
        item for item in load_checkpoint(tmp_path, TARGET_RUN).entries if item.path == "pkg/app.py"
    )
    assert entry.digest is not None
    blob = (
        tmp_path
        / CHECKPOINT_RELATIVE_ROOT
        / TARGET_RUN
        / BLOBS_DIRECTORY
        / entry.digest[:2]
        / entry.digest
    )
    blob.unlink()

    code, output = _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"])

    assert code == 1
    assert "failed: pkg/app.py (CHECKPOINT_READ_ERROR)" in output
    assert "failed=1" in output
    # Independent actions still ran.
    assert (tmp_path / "gone.txt").read_text(encoding="utf-8") == "gone\n"


def test_the_rollback_records_its_own_trace_naming_the_run_it_undid(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"])

    summaries = list_traces(tmp_path)
    assert len(summaries) == 1
    operation = summaries[0]
    # A fresh identifier: the target run's trace was closed and must stay closed.
    assert operation.run_id != TARGET_RUN
    assert operation.status == "rollback"
    assert operation.trace_complete

    trace = read_trace(tmp_path, operation.run_id)
    kinds = [event.event_type for event in trace.events]
    assert kinds == [EventType.ROLLBACK, EventType.TERMINATION]
    assert trace.events[0].payload["target_run_id"] == TARGET_RUN
    assert trace.events[-1].payload["termination_reason"] == "rollback"
    assert trace.events[-1].payload["target_run_id"] == TARGET_RUN
    assert SENSITIVE_SENTINEL not in (tmp_path / Path(trace.trace_path)).read_text(encoding="utf-8")


def test_delete_frees_a_checkpoint(tmp_path: Path) -> None:
    _workspace(tmp_path)

    code, output = _invoke(["rollback", "delete", "--workspace", str(tmp_path), TARGET_RUN])

    assert code == 0
    assert "CHECKPOINT deleted" in output
    assert "freed_bytes=" in output
    assert (
        _invoke(["rollback", "list", "--workspace", str(tmp_path)])[1].strip().endswith("readable")
    )


def test_a_missing_checkpoint_reports_a_stable_code(tmp_path: Path) -> None:
    code, output = _invoke(["rollback", "show", "--workspace", str(tmp_path), TARGET_RUN])

    assert code == 1
    assert "CHECKPOINT_NOT_FOUND" in output


def test_an_invalid_run_id_is_rejected_before_any_lookup(tmp_path: Path) -> None:
    code, output = _invoke(["rollback", "show", "--workspace", str(tmp_path), "not-a-run-id"])

    assert code == 1
    assert "INVALID_RUN_ID" in output


def test_a_missing_workspace_is_rejected(tmp_path: Path) -> None:
    code, output = _invoke(
        ["rollback", "list", "--workspace", str(tmp_path / "missing")],
    )

    assert code == 2
    assert "workspace must be an existing directory" in output


def test_long_plans_are_listed_within_a_bound(tmp_path: Path) -> None:
    create_checkpoint(tmp_path, TARGET_RUN)
    for index in range(60):
        (tmp_path / f"generated{index:03d}.txt").write_text("new\n", encoding="utf-8")

    code, output = _invoke(["rollback", "show", "--workspace", str(tmp_path), TARGET_RUN])

    listed = [line for line in output.splitlines() if line.startswith("  delete: generated")]
    assert code == 0
    assert len(listed) == 50
    assert "delete: ... and 10 more" in output


def test_interruption_during_a_rollback_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    def interrupt(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr("proofcoder.cli.perform_rollback", interrupt)
    code, output = _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"])

    assert code == 130
    assert "ROLLBACK interrupted" in output


def test_a_rollback_still_runs_when_its_trace_cannot_be_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proofcoder.rollback import build_rollback_plan, perform_rollback
    from proofcoder.trace import TracePathError

    _workspace(tmp_path)
    _change(tmp_path)

    def failing_recorder(*args: object, **kwargs: object) -> object:
        raise TracePathError("TRACE_PATH_UNAVAILABLE", "no trace here")

    monkeypatch.setattr(rollback_module, "TraceRecorder", failing_recorder)
    recorded = perform_rollback(tmp_path, build_rollback_plan(tmp_path, TARGET_RUN))

    # Losing the record is not a reason to leave the workspace as the run left it.
    assert recorded.result.complete
    assert not recorded.trace_complete
    assert not recorded.complete
    assert recorded.trace_path == ""
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_a_checkpoint_lost_between_plan_and_apply_closes_the_trace_and_reports(
    tmp_path: Path,
) -> None:
    from proofcoder.checkpoint import CheckpointError, delete_checkpoint
    from proofcoder.rollback import build_rollback_plan, perform_rollback

    _workspace(tmp_path)
    _change(tmp_path)
    plan = build_rollback_plan(tmp_path, TARGET_RUN)
    delete_checkpoint(tmp_path, TARGET_RUN)

    with pytest.raises(CheckpointError) as error:
        perform_rollback(tmp_path, plan)

    assert error.value.code == "CHECKPOINT_NOT_FOUND"
    # No half-written operation trace is left claiming a rollback happened.
    assert [summary.trace_complete for summary in list_traces(tmp_path)] == [False]
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_the_command_surfaces_that_lost_checkpoint(tmp_path: Path) -> None:
    from proofcoder.checkpoint import delete_checkpoint

    _workspace(tmp_path)
    _change(tmp_path)
    original = rollback_module.apply_rollback

    def vanish(workspace: Path, plan: object) -> object:
        delete_checkpoint(workspace, TARGET_RUN)
        return original(workspace, plan)

    rollback_module.apply_rollback = vanish
    try:
        code, output = _invoke(
            ["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"]
        )
    finally:
        rollback_module.apply_rollback = original

    assert code == 1
    assert "CHECKPOINT_NOT_FOUND" in output


def test_an_interrupted_rollback_closes_its_trace_and_can_be_resumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.checkpoint as checkpoint_module
    from proofcoder.rollback import build_rollback_plan, perform_rollback

    _workspace(tmp_path)
    _change(tmp_path)
    original = checkpoint_module._apply_action
    applied: list[str] = []

    def interrupt_after_first(*args: object, **kwargs: object) -> None:
        if applied:
            raise KeyboardInterrupt
        applied.append("one")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(checkpoint_module, "_apply_action", interrupt_after_first)
    with pytest.raises(KeyboardInterrupt):
        perform_rollback(tmp_path, build_rollback_plan(tmp_path, TARGET_RUN))
    monkeypatch.undo()

    # The trace was closed rather than left open, and records no completed rollback.
    assert [summary.trace_complete for summary in list_traces(tmp_path)] == [False]
    # The checkpoint survives and rollback is idempotent, so a rerun finishes the job.
    code, _ = _invoke(["rollback", "apply", "--workspace", str(tmp_path), TARGET_RUN, "--yes"])
    assert code == 0
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "gone.txt").read_text(encoding="utf-8") == "gone\n"
    assert not (tmp_path / "build").exists()
