"""Offline tests for evaluation execution, evidence scoring, and aggregation."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from proofcoder.context import MessageHistory
from proofcoder.eval_core import (
    EvaluationAttemptResult,
    EvaluationFailureReason,
    WorkspaceSnapshotError,
    aggregate_evaluation_results,
    compare_snapshots,
    run_evaluation_attempt,
    snapshot_workspace,
)
from proofcoder.eval_fixtures import (
    EvalFixture,
    FixtureCategory,
    FixtureValidation,
    load_fixtures,
)
from proofcoder.protocol import CompletionStatus, RunResult, TerminationReason

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "evals" / "fixtures"


def _bug_fixture() -> EvalFixture:
    return next(
        fixture
        for fixture in load_fixtures(FIXTURES_ROOT)
        if fixture.fixture_id == "bugfix-inclusive-total"
    )


def _environment(temp_directory: Path) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(temp_directory),
        "TMP": str(temp_directory),
        "PROOFCODER_TEST_SENTINEL": "kept",
        "DEEPSEEK_API_KEY": "must-not-reach-child",
    }
    for name in ("PATHEXT", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def _run_result(
    status: CompletionStatus = CompletionStatus.COMPLETED_VERIFIED,
) -> RunResult:
    return RunResult(
        termination_reason=TerminationReason.FINISH_TASK,
        final_text="done",
        history=MessageHistory(),
        model_call_count=3,
        tool_call_count=5,
        tool_error_count=1,
        completion_status=status,
        elapsed_seconds=1.25,
        api_attempt_count=4,
        api_retry_count=1,
        context_compaction_count=2,
        input_token_count=120,
        output_token_count=30,
        run_id="run-test",
        trace_path=".proofcoder/traces/run-test.jsonl",
        trace_complete=True,
    )


def _change_bug_workspace(
    workspace: Path,
    *,
    fix_source: bool = True,
    change_test: bool = True,
    unexpected: bool = False,
) -> None:
    source = workspace / "inclusive_total.py"
    source_text = source.read_text(encoding="utf-8")
    if fix_source:
        source_text = source_text.replace(
            "return sum(range(start, end))", "return sum(range(start, end + 1))"
        )
    else:
        source_text += "\n# The original defect remains.\n"
    source.write_text(source_text, encoding="utf-8")

    if change_test:
        test_path = workspace / "tests" / "test_inclusive_total.py"
        test_text = test_path.read_text(encoding="utf-8")
        marker = '\n\nif __name__ == "__main__":'
        regression = (
            "\n    def test_single_value_interval(self) -> None:\n"
            "        self.assertEqual(inclusive_total(3, 3), 3)\n"
        )
        test_path.write_text(test_text.replace(marker, regression + marker), encoding="utf-8")
    if unexpected:
        (workspace / "unexpected.txt").write_text("outside declared scope\n", encoding="utf-8")


def _transition_fixture(tmp_path: Path) -> EvalFixture:
    source = tmp_path / "transition-source"
    tests = source / "tests"
    tests.mkdir(parents=True)
    (source / "old.txt").write_text("old\n", encoding="utf-8")
    (tests / "test_transition.py").write_text(
        """import unittest
from pathlib import Path


class TransitionTests(unittest.TestCase):
    def test_transition(self) -> None:
        self.assertTrue(Path("new.txt").is_file())
        self.assertFalse(Path("old.txt").exists())


if __name__ == "__main__":
    unittest.main()
""",
        encoding="utf-8",
    )
    return EvalFixture(
        fixture_id="transition-files",
        category=FixtureCategory.CROSS_FILE_CHANGE,
        task="Replace the obsolete file with the new representation.",
        validation=FixtureValidation(
            argv=("python", "-m", "unittest", "discover", "-s", "tests", "-v"),
            cwd=".",
            success_exit_code=0,
            initial_exit_code=1,
            initial_output_contains="FAIL: test_transition",
        ),
        allowed_modified_files=("new.txt", "old.txt"),
        required_modified_files=("new.txt", "old.txt"),
        workspace_files=("old.txt", "tests/test_transition.py"),
        source_workspace=source,
    )


def test_success_records_independent_evidence_file_scope_and_run_statistics(
    tmp_path: Path,
) -> None:
    fixture = _bug_fixture()

    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        _change_bug_workspace(workspace)
        return _run_result()

    result = run_evaluation_attempt(
        fixture,
        tmp_path / "workspace",
        2,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is True
    assert result.failure_reasons == ()
    assert result.fixture_id == fixture.fixture_id
    assert result.category is FixtureCategory.BUG_FIX
    assert result.attempt_index == 2
    assert result.completion_status is CompletionStatus.COMPLETED_VERIFIED
    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.added_files == ()
    assert result.modified_files == (
        "inclusive_total.py",
        "tests/test_inclusive_total.py",
    )
    assert result.deleted_files == ()
    assert result.changed_files == result.modified_files
    assert result.unexpected_files == ()
    assert result.missing_required_files == ()
    assert result.initial_validation is not None
    assert result.final_validation is not None
    assert result.initial_validation.argv == fixture.validation.argv
    assert result.final_validation.argv == fixture.validation.argv
    assert result.initial_validation.cwd == result.final_validation.cwd == "."
    assert result.initial_validation.exit_code == fixture.validation.initial_exit_code
    assert result.final_validation.exit_code == fixture.validation.success_exit_code
    assert result.initial_validation.timed_out is False
    assert result.final_validation.timed_out is False
    assert not hasattr(result.final_validation, "stdout")
    assert not any(path.startswith(".proofcoder/") for path in result.changed_files)
    assert (
        result.model_call_count,
        result.tool_call_count,
        result.tool_error_count,
        result.api_attempt_count,
        result.api_retry_count,
        result.context_compaction_count,
        result.input_token_count,
        result.output_token_count,
        result.elapsed_seconds,
    ) == (3, 5, 1, 4, 1, 2, 120, 30, 1.25)
    assert result.run_id == "run-test"
    assert result.trace_path == ".proofcoder/traces/run-test.jsonl"
    assert result.trace_complete is True


def test_independent_validation_failure_overrides_verified_completion(tmp_path: Path) -> None:
    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        _change_bug_workspace(workspace, fix_source=False)
        return _run_result()

    result = run_evaluation_attempt(
        _bug_fixture(),
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (EvaluationFailureReason.FINAL_VALIDATION_FAILED,)
    assert result.final_validation is not None
    assert result.final_validation.exit_code == 1
    assert result.missing_required_files == ()


def test_unverified_completion_fails_even_when_independent_validation_passes(
    tmp_path: Path,
) -> None:
    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        _change_bug_workspace(workspace)
        return _run_result(CompletionStatus.COMPLETED_UNVERIFIED)

    result = run_evaluation_attempt(
        _bug_fixture(),
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (EvaluationFailureReason.COMPLETION_NOT_VERIFIED,)
    assert result.final_validation is not None
    assert result.final_validation.exit_code == 0


def test_missing_required_file_is_reported_from_observed_changes(tmp_path: Path) -> None:
    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        _change_bug_workspace(workspace, change_test=False)
        return _run_result()

    result = run_evaluation_attempt(
        _bug_fixture(),
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (EvaluationFailureReason.MISSING_REQUIRED_FILES,)
    assert result.missing_required_files == ("tests/test_inclusive_total.py",)
    assert result.final_validation is not None
    assert result.final_validation.exit_code == 0


def test_unexpected_created_file_is_reported(tmp_path: Path) -> None:
    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        _change_bug_workspace(workspace, unexpected=True)
        return _run_result()

    result = run_evaluation_attempt(
        _bug_fixture(),
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (EvaluationFailureReason.UNEXPECTED_FILES,)
    assert result.added_files == ("unexpected.txt",)
    assert result.unexpected_files == ("unexpected.txt",)


def test_created_and_deleted_required_files_can_succeed(tmp_path: Path) -> None:
    fixture = _transition_fixture(tmp_path)

    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        (workspace / "old.txt").unlink()
        (workspace / "new.txt").write_text("new\n", encoding="utf-8")
        return _run_result()

    result = run_evaluation_attempt(
        fixture,
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is True
    assert result.added_files == ("new.txt",)
    assert result.modified_files == ()
    assert result.deleted_files == ("old.txt",)
    assert result.changed_files == ("new.txt", "old.txt")


@pytest.mark.parametrize(
    ("validation", "expected_reason"),
    [
        (
            FixtureValidation(
                argv=("python", "-m", "unittest", "discover", "-s", "tests", "-v"),
                cwd=".",
                success_exit_code=0,
                initial_exit_code=2,
                initial_output_contains="FAIL: test_includes_upper_bound",
            ),
            EvaluationFailureReason.INITIAL_EXIT_CODE_MISMATCH,
        ),
        (
            FixtureValidation(
                argv=("python", "-m", "unittest", "discover", "-s", "tests", "-v"),
                cwd=".",
                success_exit_code=0,
                initial_exit_code=1,
                initial_output_contains="a deliberately absent marker",
            ),
            EvaluationFailureReason.INITIAL_OUTPUT_MISMATCH,
        ),
    ],
)
def test_bad_initial_fixture_state_stops_before_runner(
    tmp_path: Path,
    validation: FixtureValidation,
    expected_reason: EvaluationFailureReason,
) -> None:
    fixture = replace(_bug_fixture(), validation=validation)
    called = False

    def runner(_fixture: EvalFixture, _workspace: Path) -> RunResult:
        nonlocal called
        called = True
        return _run_result()

    result = run_evaluation_attempt(
        fixture,
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (expected_reason,)
    assert called is False
    assert result.final_validation is None


def test_workspace_symlink_created_by_runner_is_an_infrastructure_failure(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")

    def runner(_fixture: EvalFixture, workspace: Path) -> RunResult:
        _change_bug_workspace(workspace)
        try:
            (workspace / "linked.txt").symlink_to(outside)
        except OSError as error:
            pytest.skip(f"file symlinks unavailable: {error}")
        return _run_result()

    result = run_evaluation_attempt(
        _bug_fixture(),
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (EvaluationFailureReason.SNAPSHOT_ERROR,)
    assert result.final_validation is None


def test_runner_exception_is_stable_and_does_not_run_final_validation(tmp_path: Path) -> None:
    def runner(_fixture: EvalFixture, _workspace: Path) -> RunResult:
        raise RuntimeError("fake runner failure with internal detail")

    result = run_evaluation_attempt(
        _bug_fixture(),
        tmp_path / "workspace",
        1,
        runner,
        environ=_environment(tmp_path),
    )

    assert result.success is False
    assert result.failure_reasons == (
        EvaluationFailureReason.MISSING_REQUIRED_FILES,
        EvaluationFailureReason.RUNNER_ERROR,
    )
    assert result.final_validation is None
    assert result.run_id == ""


def test_snapshot_uses_posix_sha256_ignores_internal_files_and_tracks_all_changes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    internal = workspace / ".proofcoder" / "traces"
    nested.mkdir(parents=True)
    internal.mkdir(parents=True)
    (workspace / "delete.txt").write_text("delete\n", encoding="utf-8")
    (nested / "value.txt").write_text("before\n", encoding="utf-8")
    (internal / "secret.jsonl").write_text("ignored body\n", encoding="utf-8")

    before = snapshot_workspace(workspace)
    expected_hash = hashlib.sha256((nested / "value.txt").read_bytes()).hexdigest()
    assert [(item.path, item.sha256) for item in before.files] == [
        (
            "delete.txt",
            hashlib.sha256((workspace / "delete.txt").read_bytes()).hexdigest(),
        ),
        ("nested/value.txt", expected_hash),
    ]
    assert all(len(item.sha256) == 64 for item in before.files)
    assert not hasattr(before.files[0], "content")

    (workspace / "delete.txt").unlink()
    (nested / "value.txt").write_text("after\n", encoding="utf-8")
    (workspace / "add.txt").write_text("add\n", encoding="utf-8")
    changes = compare_snapshots(before, snapshot_workspace(workspace))

    assert changes.added_files == ("add.txt",)
    assert changes.modified_files == ("nested/value.txt",)
    assert changes.deleted_files == ("delete.txt",)


def test_snapshot_rejects_symlink_directly(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        (workspace / "link.txt").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    with pytest.raises(WorkspaceSnapshotError) as captured:
        snapshot_workspace(workspace)

    assert captured.value.code == "SNAPSHOT_SYMLINK"


def test_nonempty_target_becomes_a_materialization_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("keep\n", encoding="utf-8")

    result = run_evaluation_attempt(
        _bug_fixture(),
        workspace,
        1,
        lambda _fixture, _workspace: _run_result(),
        environ=_environment(tmp_path),
    )

    assert result.failure_reasons == (EvaluationFailureReason.MATERIALIZATION_ERROR,)
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_aggregation_is_deterministic_and_sums_each_dimension() -> None:
    first = EvaluationAttemptResult(
        fixture_id="fixture-b",
        category=FixtureCategory.FEATURE_ADDITION,
        attempt_index=2,
        success=False,
        failure_reasons=(EvaluationFailureReason.FINAL_VALIDATION_FAILED,),
        model_call_count=2,
        tool_call_count=5,
        tool_error_count=1,
        api_attempt_count=3,
        api_retry_count=1,
        context_compaction_count=1,
        input_token_count=100,
        output_token_count=20,
        elapsed_seconds=0.2,
        run_id="run-b",
    )
    second = EvaluationAttemptResult(
        fixture_id="fixture-a",
        category=FixtureCategory.BUG_FIX,
        attempt_index=1,
        success=True,
        failure_reasons=(),
        model_call_count=4,
        tool_call_count=7,
        api_attempt_count=4,
        input_token_count=200,
        output_token_count=40,
        elapsed_seconds=0.1,
        run_id="run-a",
    )
    third = replace(
        first,
        attempt_index=1,
        success=True,
        failure_reasons=(),
        model_call_count=1,
        tool_call_count=2,
        tool_error_count=0,
        api_attempt_count=1,
        api_retry_count=0,
        context_compaction_count=2,
        input_token_count=50,
        output_token_count=10,
        elapsed_seconds=0.3,
        run_id="run-c",
    )

    aggregate = aggregate_evaluation_results((first, second, third))

    assert aggregate == aggregate_evaluation_results((third, first, second))
    assert [item.fixture_id for item in aggregate.fixtures] == ["fixture-a", "fixture-b"]
    assert [item.category for item in aggregate.categories] == [
        FixtureCategory.BUG_FIX,
        FixtureCategory.FEATURE_ADDITION,
    ]
    assert aggregate.fixtures[1].metrics.attempts == 2
    assert aggregate.fixtures[1].metrics.successes == 1
    assert aggregate.fixtures[1].metrics.success_rate == 0.5
    assert aggregate.overall.attempts == 3
    assert aggregate.overall.successes == 2
    assert aggregate.overall.success_rate == pytest.approx(2 / 3)
    assert (
        aggregate.overall.model_calls,
        aggregate.overall.tool_calls,
        aggregate.overall.tool_errors,
        aggregate.overall.api_attempts,
        aggregate.overall.api_retries,
        aggregate.overall.context_compactions,
        aggregate.overall.input_tokens,
        aggregate.overall.output_tokens,
        aggregate.overall.elapsed_seconds,
    ) == (7, 14, 1, 8, 1, 3, 350, 70, 0.6)


def test_aggregation_rejects_one_fixture_id_in_multiple_categories() -> None:
    result = EvaluationAttemptResult(
        fixture_id="same-id",
        category=FixtureCategory.BUG_FIX,
        attempt_index=1,
        success=True,
        failure_reasons=(),
    )

    with pytest.raises(ValueError, match="multiple categories"):
        aggregate_evaluation_results(
            (result, replace(result, category=FixtureCategory.FEATURE_ADDITION))
        )


def test_attempt_index_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        run_evaluation_attempt(
            _bug_fixture(),
            tmp_path / "workspace",
            0,
            lambda _fixture, _workspace: _run_result(),
        )
