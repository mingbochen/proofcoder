"""Offline tests for repeated evaluation orchestration and persistence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from proofcoder import cli
from proofcoder.agent_runtime import AgentRunLimits
from proofcoder.config import ProofCoderConfig
from proofcoder.context import MessageHistory
from proofcoder.errors import ProofCoderError
from proofcoder.eval_core import (
    AgentRunner,
    EvaluationAttemptInfrastructureError,
    EvaluationFailureReason,
)
from proofcoder.eval_fixtures import EvalFixture
from proofcoder.eval_runner import (
    EvaluationInfrastructureError,
    EvaluationModelInfo,
    EvaluationProgress,
    EvaluationStatus,
    create_evaluation_agent_runner,
    run_evaluation,
)
from proofcoder.events import EventEmitter, EventType
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import (
    CompletionStatus,
    FunctionCall,
    ModelResponse,
    RunResult,
    TerminationReason,
    ToolCall,
)
from proofcoder.trace import TraceRecorder

REPOSITORY_FIXTURES = Path(__file__).resolve().parents[2] / "evals" / "fixtures"
FIXED_TIME = datetime(2026, 8, 29, 1, 2, 3, 456789, tzinfo=UTC)
SECRET = "eval-api-key-must-not-leak"
REASONING = "private-evaluation-reasoning-must-not-leak"
MODEL = EvaluationModelInfo(
    name="deepseek-test",
    base_url="https://example.invalid",
    reasoning_effort="high",
)
LIMITS = AgentRunLimits(max_steps=4, max_seconds=30.0)


def _project(tmp_path: Path) -> Path:
    suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:8]
    root = tmp_path.parent / f"p{suffix}"
    root.mkdir()
    shutil.copytree(REPOSITORY_FIXTURES, root / "evals" / "fixtures")
    return root


def _environment(root: Path) -> dict[str, str]:
    environment = {
        "DEEPSEEK_API_KEY": SECRET,
        "PATH": str(Path(sys.executable).resolve().parent),
        "TEMP": str(root),
        "TMP": str(root),
    }
    for name in ("PATHEXT", "SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    return environment


def _modify_fixture(fixture: EvalFixture, workspace: Path, *, valid: bool = True) -> None:
    if fixture.fixture_id == "bugfix-inclusive-total":
        source = workspace / "inclusive_total.py"
        text = source.read_text(encoding="utf-8")
        if valid:
            text = text.replace("range(start, end)", "range(start, end + 1)")
        source.write_text(text + "\n# fake runner source evidence\n", encoding="utf-8")
        test = workspace / "tests" / "test_inclusive_total.py"
    elif fixture.fixture_id == "feature-available-items":
        source = workspace / "inventory.py"
        text = source.read_text(encoding="utf-8")
        if valid:
            text = text.replace(
                "[name for name, _ in items]", "[name for name, count in items if count > 0]"
            )
        source.write_text(text + "\n# fake runner source evidence\n", encoding="utf-8")
        test = workspace / "tests" / "test_inventory.py"
    else:
        settings = workspace / "settings.py"
        settings.write_text(
            settings.read_text(encoding="utf-8") + '\nDEFAULT_SEPARATOR = " | "\n',
            encoding="utf-8",
        )
        source = workspace / "message.py"
        text = source.read_text(encoding="utf-8")
        if valid:
            text = text.replace(
                "from settings import DEFAULT_PREFIX",
                "from settings import DEFAULT_PREFIX, DEFAULT_SEPARATOR",
            ).replace(
                'return f"{DEFAULT_PREFIX}: {message}"',
                'return f"{DEFAULT_PREFIX}{DEFAULT_SEPARATOR}{message}"',
            )
        source.write_text(text + "\n# fake runner source evidence\n", encoding="utf-8")
        test = workspace / "tests" / "test_message.py"
    test.write_text(
        test.read_text(encoding="utf-8") + "\n# fake runner regression evidence\n",
        encoding="utf-8",
    )


def _traced_result(
    fixture: EvalFixture,
    workspace: Path,
    run_number: int,
    *,
    completion: CompletionStatus | None = CompletionStatus.COMPLETED_VERIFIED,
    termination: TerminationReason = TerminationReason.FINISH_TASK,
    trace: bool = True,
    elapsed_seconds: float = 0.25,
) -> RunResult:
    run_id = f"{run_number:032x}"
    trace_path: str | None = None
    if trace:
        recorder = TraceRecorder(workspace, run_id, sensitive_values=(SECRET,))
        emitter = EventEmitter(run_id=run_id, sink=recorder, sensitive_values=(SECRET,))
        emitter.emit(EventType.TASK, step=0, payload={"task": fixture.task})
        emitter.emit(
            EventType.TERMINATION,
            step=1,
            payload={
                "termination_reason": termination.value,
                "completion_status": "none" if completion is None else completion.value,
                "trace_complete": True,
            },
        )
        trace_path = recorder.trace_path
        recorder.close()

    history = MessageHistory()
    history.add_system("offline fake system")
    history.add_user(fixture.task)
    history.add_assistant(
        ModelResponse(
            content=f"final text {SECRET}",
            reasoning_content=REASONING,
            finish_reason="stop",
            usage=None,
        )
    )
    return RunResult(
        termination_reason=termination,
        final_text=f"final text {SECRET}",
        history=history,
        model_call_count=2,
        tool_call_count=4,
        tool_error_count=1,
        completion_status=completion,
        api_attempt_count=3,
        api_retry_count=1,
        context_compaction_count=1,
        input_token_count=50,
        output_token_count=10,
        elapsed_seconds=elapsed_seconds,
        run_id=run_id,
        trace_path=trace_path,
        trace_complete=trace,
    )


def _successful_runner(
    calls: list[tuple[str, Path]],
    *,
    outcome: Callable[[int], tuple[CompletionStatus | None, TerminationReason]] | None = None,
    valid: Callable[[int], bool] | None = None,
    trace: bool = True,
    elapsed_seconds: float = 0.25,
) -> AgentRunner:
    def runner(fixture: EvalFixture, workspace: Path) -> RunResult:
        run_number = len(calls) + 1
        calls.append((fixture.fixture_id, workspace))
        _modify_fixture(fixture, workspace, valid=True if valid is None else valid(run_number))
        completion, termination = (
            (CompletionStatus.COMPLETED_VERIFIED, TerminationReason.FINISH_TASK)
            if outcome is None
            else outcome(run_number)
        )
        return _traced_result(
            fixture,
            workspace,
            run_number,
            completion=completion,
            termination=termination,
            trace=trace,
            elapsed_seconds=elapsed_seconds,
        )

    return runner


def _run(
    root: Path,
    runner: AgentRunner,
    *,
    eval_id: str = "a" * 32,
    repeat: int = 1,
    fixture_ids: tuple[str, ...] = (),
    output_root: Path = Path("o"),
    on_progress: Callable[[EvaluationProgress], None] | None = None,
):
    return run_evaluation(
        project_root=root,
        fixture_ids=fixture_ids,
        repeat=repeat,
        output_root=output_root,
        agent_runner=runner,
        model=MODEL,
        limits=LIMITS,
        environ=_environment(root),
        eval_id_factory=lambda: eval_id,
        clock=lambda: FIXED_TIME,
        on_progress=on_progress,
    )


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_three_fixtures_repeat_two_are_ordered_isolated_and_fully_persisted(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []
    progress: list[EvaluationProgress] = []

    session = _run(
        root,
        _successful_runner(calls),
        repeat=2,
        on_progress=progress.append,
    )

    expected_order = [
        "bugfix-inclusive-total",
        "bugfix-inclusive-total",
        "cross-file-message-format",
        "cross-file-message-format",
        "feature-available-items",
        "feature-available-items",
    ]
    assert session.status is EvaluationStatus.COMPLETED
    assert session.exit_code == 0
    assert [fixture_id for fixture_id, _ in calls] == expected_order
    assert len({workspace for _, workspace in calls}) == 6
    assert all(workspace.name == "w" for _, workspace in calls)
    assert all(workspace.is_dir() for _, workspace in calls)
    assert len({attempt.run_id for attempt in session.attempts}) == 6
    assert all(attempt.trace_complete for attempt in session.attempts)

    evaluation = session.evaluation_directory
    metadata = _json(evaluation / "metadata.json")
    summary = _json(evaluation / "summary.json")
    lines = (evaluation / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    attempts = [json.loads(line) for line in lines]
    assert metadata["schema_version"] == 1
    assert metadata["eval_id"] == "a" * 32
    assert metadata["repeat"] == 2
    assert metadata["code"] == {"dirty": None, "revision": None}
    assert metadata["warnings"] == ["GIT_REVISION_UNAVAILABLE", "GIT_DIRTY_UNAVAILABLE"]
    assert summary["status"] == "completed"
    assert summary["recorded_attempts"] == summary["expected_attempts"] == 6
    assert summary["overall"]["successes"] == 6
    assert [item["sequence"] for item in attempts] == list(range(1, 7))
    assert len({(item["fixture_id"], item["attempt"]) for item in attempts}) == 6
    assert [item["fixture_id"] for item in attempts] == expected_order
    assert all(not Path(item["workspace"]).is_absolute() for item in attempts)
    assert all(item["files"]["ignored_runtime"] for item in attempts)
    assert all(
        item["files"]["ignored_runtime"] == sorted(item["files"]["ignored_runtime"])
        for item in attempts
    )
    assert all(
        not set(item["files"]["ignored_runtime"]) & set(item["files"]["changed"])
        for item in attempts
    )
    assert all(
        "stdout" not in evidence and "stderr" not in evidence
        for item in attempts
        for evidence in item["validation"].values()
        if evidence is not None
    )
    assert all((evaluation / item["trace_path"]).is_file() for item in attempts)
    assert progress[0].eval_id == session.eval_id
    assert progress[-1].status is EvaluationStatus.COMPLETED
    for filename in ("metadata.json", "attempts.jsonl", "summary.json"):
        content = (evaluation / filename).read_bytes()
        assert content.endswith(b"\n")
        assert b"\r\n" not in content


def test_selected_fixture_input_order_does_not_change_execution_order(tmp_path: Path) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    _run(
        root,
        _successful_runner(calls),
        eval_id="b" * 32,
        fixture_ids=("feature-available-items", "bugfix-inclusive-total"),
    )

    assert [fixture_id for fixture_id, _ in calls] == [
        "bugfix-inclusive-total",
        "feature-available-items",
    ]


def test_mixed_success_and_unverified_failure_continue_and_exit_one(tmp_path: Path) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    def outcomes(run_number: int) -> tuple[CompletionStatus | None, TerminationReason]:
        status = (
            CompletionStatus.COMPLETED_UNVERIFIED
            if run_number == 1
            else CompletionStatus.COMPLETED_VERIFIED
        )
        return status, TerminationReason.FINISH_TASK

    session = _run(
        root,
        _successful_runner(
            calls,
            outcome=outcomes,
            elapsed_seconds=76.93899999999849,
        ),
        repeat=2,
        fixture_ids=("bugfix-inclusive-total",),
    )

    assert len(calls) == 2
    assert session.status is EvaluationStatus.COMPLETED
    assert session.exit_code == 1
    assert [attempt.success for attempt in session.attempts] == [False, True]
    assert EvaluationFailureReason.COMPLETION_NOT_VERIFIED in session.attempts[0].failure_reasons
    summary = _json(session.evaluation_directory / "summary.json")
    assert summary["overall"]["attempts"] == 2
    assert summary["overall"]["successes"] == 1
    expected_reasons = {"completion_not_verified": 1}
    assert summary["fixtures"][0]["failure_reason_counts"] == expected_reasons
    assert summary["categories"][0]["failure_reason_counts"] == expected_reasons
    assert summary["overall"]["failure_reason_counts"] == expected_reasons
    assert list(summary["overall"]["failure_reason_counts"]) == sorted(expected_reasons)
    assert summary["overall"]["elapsed_seconds"] == 153.878
    records = [
        json.loads(line)
        for line in (session.evaluation_directory / "attempts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [record["statistics"]["elapsed_seconds"] for record in records] == [
        76.939,
        76.939,
    ]
    assert expected_reasons == {
        reason: sum(
            not record["success"] and reason in record["failure_reasons"] for record in records
        )
        for reason in expected_reasons
    }


@pytest.mark.parametrize(
    ("completion", "termination"),
    [
        (CompletionStatus.COMPLETED_UNVERIFIED, TerminationReason.FINISH_TASK),
        (CompletionStatus.BLOCKED, TerminationReason.FINISH_TASK),
        (None, TerminationReason.API_ERROR),
        (None, TerminationReason.MODEL_STOPPED),
    ],
)
def test_agent_failure_outcomes_are_attempt_failures_not_session_failures(
    tmp_path: Path,
    completion: CompletionStatus | None,
    termination: TerminationReason,
) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    session = _run(
        root,
        _successful_runner(calls, outcome=lambda _index: (completion, termination)),
        fixture_ids=("bugfix-inclusive-total",),
    )

    assert session.status is EvaluationStatus.COMPLETED
    assert session.exit_code == 1
    assert session.attempts[0].termination_reason is termination
    record = json.loads(
        (session.evaluation_directory / "attempts.jsonl").read_text(encoding="utf-8")
    )
    assert record["termination_reason"] == termination.value


def test_independent_validation_failure_reasons_are_aggregated_and_recorded(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    session = _run(
        root,
        _successful_runner(
            calls,
            valid=lambda _index: False,
            outcome=lambda _index: (
                CompletionStatus.COMPLETED_UNVERIFIED,
                TerminationReason.FINISH_TASK,
            ),
        ),
        fixture_ids=("bugfix-inclusive-total",),
    )

    attempt = session.attempts[0]
    assert attempt.success is False
    assert attempt.final_validation is not None
    assert attempt.final_validation.exit_code != 0
    assert EvaluationFailureReason.FINAL_VALIDATION_FAILED in attempt.failure_reasons
    expected_reasons = {
        "completion_not_verified": 1,
        "final_validation_failed": 1,
    }
    summary = _json(session.evaluation_directory / "summary.json")
    assert summary["fixtures"][0]["failure_reason_counts"] == expected_reasons
    assert summary["categories"][0]["failure_reason_counts"] == expected_reasons
    assert summary["overall"]["failure_reason_counts"] == expected_reasons
    assert list(summary["overall"]["failure_reason_counts"]) == sorted(expected_reasons)
    record = json.loads(
        (session.evaluation_directory / "attempts.jsonl").read_text(encoding="utf-8")
    )
    assert set(record["failure_reasons"]) == set(expected_reasons)


@pytest.mark.parametrize(
    ("fixture_ids", "code"),
    [
        (("missing-fixture",), "UNKNOWN_FIXTURE"),
        (("bugfix-inclusive-total", "bugfix-inclusive-total"), "DUPLICATE_FIXTURE"),
    ],
)
def test_unknown_and_duplicate_fixture_ids_fail_before_runner(
    tmp_path: Path, fixture_ids: tuple[str, ...], code: str
) -> None:
    root = _project(tmp_path)
    called = False

    def forbidden(_fixture: EvalFixture, _workspace: Path) -> RunResult:
        nonlocal called
        called = True
        raise AssertionError

    with pytest.raises(EvaluationInfrastructureError) as captured:
        _run(root, forbidden, fixture_ids=fixture_ids)

    assert captured.value.code == code
    assert called is False
    assert not (root / ".proofcoder" / "evals").exists()


@pytest.mark.parametrize("repeat", [0, 11])
def test_repeat_boundaries_fail_before_runner(tmp_path: Path, repeat: int) -> None:
    root = _project(tmp_path)

    with pytest.raises(EvaluationInfrastructureError) as captured:
        _run(root, lambda _fixture, _workspace: pytest.fail("runner called"), repeat=repeat)

    assert captured.value.code == "INVALID_REPEAT"


def test_keyboard_interrupt_preserves_completed_jsonl_and_current_workspace(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []
    success = _successful_runner(calls)

    def interrupt_second(fixture: EvalFixture, workspace: Path) -> RunResult:
        if len(calls) == 1:
            calls.append((fixture.fixture_id, workspace))
            raise KeyboardInterrupt
        return success(fixture, workspace)

    session = _run(
        root,
        interrupt_second,
        repeat=2,
        fixture_ids=("bugfix-inclusive-total",),
    )

    assert session.status is EvaluationStatus.INTERRUPTED
    assert session.exit_code == 130
    assert len(session.attempts) == 1
    assert len(calls) == 2
    assert calls[1][1].is_dir()
    assert (
        len(
            (session.evaluation_directory / "attempts.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        == 1
    )
    summary = _json(session.evaluation_directory / "summary.json")
    assert summary["status"] == "interrupted"
    assert summary["recorded_attempts"] == 1


def test_incomplete_trace_is_a_local_attempt_failure(tmp_path: Path) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    session = _run(
        root,
        _successful_runner(calls, trace=False),
        fixture_ids=("bugfix-inclusive-total",),
    )

    assert session.exit_code == 1
    assert session.attempts[0].trace_complete is False
    assert EvaluationFailureReason.TRACE_INCOMPLETE in session.attempts[0].failure_reasons


def test_interrupted_agent_result_is_persisted_and_stops_before_next_attempt(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    session = _run(
        root,
        _successful_runner(
            calls,
            outcome=lambda _index: (None, TerminationReason.INTERRUPTED),
        ),
        repeat=2,
        fixture_ids=("bugfix-inclusive-total",),
    )

    assert session.status is EvaluationStatus.INTERRUPTED
    assert session.exit_code == 130
    assert len(calls) == 1
    assert len(session.attempts) == 1
    assert session.attempts[0].final_validation is None
    assert session.attempts[0].trace_complete is True
    record = json.loads(
        (session.evaluation_directory / "attempts.jsonl").read_text(encoding="utf-8")
    )
    assert record["termination_reason"] == "interrupted"


def test_result_write_failure_stops_without_reporting_unsaved_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from proofcoder import eval_runner

    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    def fail_append(self: object, payload: object) -> None:
        raise EvaluationInfrastructureError("RESULT_WRITE_FAILED", "injected write failure")

    monkeypatch.setattr(eval_runner._SessionWriter, "append_attempt", fail_append)
    session = _run(
        root,
        _successful_runner(calls),
        fixture_ids=("bugfix-inclusive-total",),
    )

    assert session.status is EvaluationStatus.FAILED
    assert session.exit_code == 2
    assert session.failure_code == "RESULT_WRITE_FAILED"
    assert session.attempts == ()
    assert (session.evaluation_directory / "attempts.jsonl").read_bytes() == b""
    summary = _json(session.evaluation_directory / "summary.json")
    assert summary["status"] == "failed"
    assert summary["recorded_attempts"] == 0


def test_output_outside_project_and_file_target_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    output_file = root / "result-file"
    output_file.write_text("keep\n", encoding="utf-8")

    with pytest.raises(EvaluationInfrastructureError) as outside:
        _run(
            root, lambda _fixture, _workspace: pytest.fail("runner called"), output_root=Path("..")
        )
    with pytest.raises(EvaluationInfrastructureError) as file_target:
        _run(
            root,
            lambda _fixture, _workspace: pytest.fail("runner called"),
            output_root=Path("result-file"),
        )

    assert outside.value.code == "PATH_OUTSIDE_PROJECT"
    assert file_target.value.code == "OUTPUT_ROOT_INVALID"
    assert output_file.read_text(encoding="utf-8") == "keep\n"


def test_output_symlink_is_rejected_before_runner(tmp_path: Path) -> None:
    root = _project(tmp_path)
    actual = root / "actual-output"
    actual.mkdir()
    link = root / "linked-output"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(EvaluationInfrastructureError) as captured:
        _run(
            root,
            lambda _fixture, _workspace: pytest.fail("runner called"),
            output_root=Path("linked-output"),
        )

    assert captured.value.code == "PATH_SYMLINK"
    assert not tuple(actual.iterdir())


def test_cli_eval_redacts_sensitive_values_and_uses_compact_output(tmp_path: Path) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []
    stream = io.StringIO()
    environment = _environment(root)
    environment["DEEPSEEK_MODEL"] = SECRET
    environment["DEEPSEEK_BASE_URL"] = f"https://example.invalid/{SECRET}"

    code = cli.main(
        [
            "eval",
            "--repeat",
            "1",
            "--fixture",
            "bugfix-inclusive-total",
            "--output-root",
            "o",
        ],
        environ=environment,
        cwd=root,
        console=Console(file=stream, force_terminal=False, color_system=None, width=240),
        eval_agent_runner=_successful_runner(calls),
    )

    output = stream.getvalue()
    assert code == 0
    for label in ("EVAL ", "RUN ", "RESULT ", "SUMMARY ", "ARTIFACT "):
        assert label in output
    assert "TASK:" not in output
    assert "MODEL:" not in output
    assert SECRET not in output
    assert REASONING not in output
    evaluation = next((root / "o").iterdir())
    persisted = "".join(
        (evaluation / filename).read_text(encoding="utf-8")
        for filename in ("metadata.json", "attempts.jsonl", "summary.json")
    )
    assert SECRET not in persisted
    assert REASONING not in persisted
    assert "Ran 2 tests" not in persisted


def test_cli_eval_mixed_results_continue_and_return_one(tmp_path: Path) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []
    stream = io.StringIO()

    def outcomes(run_number: int) -> tuple[CompletionStatus | None, TerminationReason]:
        completion = (
            CompletionStatus.COMPLETED_UNVERIFIED
            if run_number == 1
            else CompletionStatus.COMPLETED_VERIFIED
        )
        return completion, TerminationReason.FINISH_TASK

    code = cli.main(
        [
            "eval",
            "--repeat",
            "2",
            "--fixture",
            "bugfix-inclusive-total",
            "--output-root",
            "o",
        ],
        environ=_environment(root),
        cwd=root,
        console=Console(file=stream, force_terminal=False, color_system=None, width=240),
        eval_agent_runner=_successful_runner(
            calls,
            outcome=outcomes,
            elapsed_seconds=76.93899999999849,
        ),
    )

    assert code == 1
    assert len(calls) == 2
    output = stream.getvalue()
    assert output.count("RUN bugfix-inclusive-total") == 2
    assert "reasons=completion_not_verified" in output
    assert "reasons=none" in output
    assert output.count("elapsed_seconds=76.939") == 2
    assert "999999" not in output
    assert "SUMMARY status=completed attempts=2 successes=1" in output


def test_metadata_uses_safe_git_commands_for_revision_and_dirty_state(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    root = _project(tmp_path)
    (root / ".gitignore").write_text(".proofcoder/\no/\n", encoding="utf-8")
    subprocess.run([git, "init", "-q"], cwd=root, check=True)
    subprocess.run([git, "config", "core.autocrlf", "false"], cwd=root, check=True)
    subprocess.run([git, "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=ProofCoder Test",
            "-c",
            "user.email=proofcoder@example.invalid",
            "commit",
            "-qm",
            "fixture baseline",
        ],
        cwd=root,
        check=True,
    )
    calls: list[tuple[str, Path]] = []
    environment = _environment(root)
    environment["PATH"] = os.environ.get("PATH", "")

    session = run_evaluation(
        project_root=root,
        output_root=Path("o"),
        fixture_ids=("bugfix-inclusive-total",),
        repeat=1,
        agent_runner=_successful_runner(calls),
        model=MODEL,
        limits=LIMITS,
        environ=environment,
        eval_id_factory=lambda: "c" * 32,
        clock=lambda: FIXED_TIME,
    )

    metadata = _json(session.evaluation_directory / "metadata.json")
    assert isinstance(metadata["code"]["revision"], str)
    assert len(metadata["code"]["revision"]) == 40
    assert metadata["code"]["dirty"] is False
    assert metadata["warnings"] == []


def test_cli_eval_help_and_argument_boundaries_are_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    help_result = cli.build_parser().format_help()
    with pytest.raises(SystemExit) as captured:
        cli.build_parser().parse_args(["eval", "--help"])
    eval_help = capsys.readouterr().out

    assert "eval" in help_result
    assert captured.value.code == 0
    assert "real model calls" in " ".join(eval_help.split())
    for option in (
        "--repeat",
        "--fixture",
        "--fixtures-root",
        "--output-root",
        "--max-steps",
        "--max-seconds",
        "--context-budget-bytes",
        "--max-consecutive-failures",
        "--max-api-attempts",
    ):
        assert option in eval_help
    defaults = cli.build_parser().parse_args(["eval"])
    assert defaults.repeat == 3
    assert defaults.fixtures_root == str(Path("evals/fixtures"))
    assert defaults.output_root == str(Path(".proofcoder/evals"))


@pytest.mark.parametrize("repeat", ["0", "11"])
def test_cli_eval_rejects_repeat_boundaries(repeat: str) -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.build_parser().parse_args(["eval", "--repeat", repeat])


def test_production_eval_runner_rebuilds_loop_registry_history_and_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from proofcoder import eval_runner

    root = _project(tmp_path)
    fixture = next(
        item
        for item in eval_runner.load_fixtures(root / "evals" / "fixtures")
        if item.fixture_id == "bugfix-inclusive-total"
    )
    registries: list[object] = []
    loops: list[object] = []
    original_resources = eval_runner.create_agent_runtime_resources
    original_loop = eval_runner.build_agent_loop

    def capture_resources(*args: object, **kwargs: object):
        resources = original_resources(*args, **kwargs)
        registries.append(resources.registry)
        assert [schema["function"]["name"] for schema in resources.registry.schemas()] == [
            "list_files",
            "search_text",
            "read_file",
            "create_file",
            "replace_in_file",
            "run_command",
            "finish_task",
        ]
        return resources

    def capture_loop(*args: object, **kwargs: object):
        loop = original_loop(*args, **kwargs)
        loops.append(loop)
        return loop

    monkeypatch.setattr(eval_runner, "create_agent_runtime_resources", capture_resources)
    monkeypatch.setattr(eval_runner, "build_agent_loop", capture_loop)

    def client_factory(_config: ProofCoderConfig) -> ScriptedClient:
        finish = ToolCall(
            id="finish",
            function=FunctionCall(
                name="finish_task",
                arguments=json.dumps({"summary": "blocked", "blocked_reason": "offline fake"}),
            ),
        )
        return ScriptedClient(
            [
                ModelResponse(
                    content=None,
                    reasoning_content=REASONING,
                    finish_reason="tool_calls",
                    usage=None,
                    tool_calls=(finish,),
                )
            ]
        )

    runner = create_evaluation_agent_runner(
        config=ProofCoderConfig(api_key="fake-key"),
        limits=LIMITS,
        environ=_environment(root),
        client_factory=client_factory,
    )
    workspaces = (root / "first", root / "second")
    for workspace in workspaces:
        workspace.mkdir()
    first = runner(fixture, workspaces[0])
    second = runner(fixture, workspaces[1])

    assert registries[0] is not registries[1]
    assert loops[0] is not loops[1]
    assert first.history is not second.history
    assert first.run_id != second.run_id
    assert len(first.run_id) == len(second.run_id) == 32
    assert first.trace_path is not None
    assert second.trace_path is not None
    assert first.run_id in first.trace_path
    assert second.run_id in second.trace_path
    assert all(result.trace_complete for result in (first, second))


@pytest.mark.parametrize(
    ("failure", "termination"),
    [
        (KeyboardInterrupt(), TerminationReason.INTERRUPTED),
        (ProofCoderError("offline client failure"), TerminationReason.API_ERROR),
    ],
)
def test_production_runner_persists_client_setup_failures_without_network(
    tmp_path: Path,
    failure: BaseException,
    termination: TerminationReason,
) -> None:
    from proofcoder.eval_fixtures import load_fixtures

    root = _project(tmp_path)
    fixture = load_fixtures(root / "evals" / "fixtures")[0]
    workspace = root / "w"
    workspace.mkdir()

    def fail_client(_config: ProofCoderConfig) -> ScriptedClient:
        raise failure

    runner = create_evaluation_agent_runner(
        config=ProofCoderConfig(api_key="fake-key"),
        limits=LIMITS,
        environ=_environment(root),
        client_factory=fail_client,
    )
    result = runner(fixture, workspace)

    assert result.termination_reason is termination
    assert result.model_call_count == 0
    assert result.trace_complete is True
    assert result.trace_path is not None
    assert (workspace / result.trace_path).is_file()


def test_production_runner_converts_trace_setup_failure_to_infrastructure_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from proofcoder import eval_runner
    from proofcoder.eval_fixtures import load_fixtures

    root = _project(tmp_path)
    fixture = load_fixtures(root / "evals" / "fixtures")[0]
    workspace = root / "w"
    workspace.mkdir()

    def fail_resources(*args: object, **kwargs: object) -> object:
        raise ValueError("injected setup failure")

    monkeypatch.setattr(eval_runner, "create_agent_runtime_resources", fail_resources)
    runner = create_evaluation_agent_runner(
        config=ProofCoderConfig(api_key="fake-key"),
        limits=LIMITS,
        environ=_environment(root),
        client_factory=lambda _config: pytest.fail("client factory called"),
    )

    with pytest.raises(EvaluationAttemptInfrastructureError) as captured:
        runner(fixture, workspace)
    assert captured.value.code == "AGENT_SETUP_FAILED"


def test_invalid_eval_id_and_existing_eval_directory_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    calls: list[tuple[str, Path]] = []

    with pytest.raises(EvaluationInfrastructureError) as invalid:
        _run(root, _successful_runner(calls), eval_id="not-an-eval-id")

    output = root / "other-output"
    existing = output / ("d" * 32)
    existing.mkdir(parents=True)
    with pytest.raises(EvaluationInfrastructureError) as collision:
        _run(
            root,
            _successful_runner(calls),
            eval_id="d" * 32,
            output_root=Path("other-output"),
        )

    assert invalid.value.code == "INVALID_EVAL_ID"
    assert collision.value.code == "EVAL_DIRECTORY_UNAVAILABLE"
    assert calls == []


def test_missing_fixture_and_invalid_project_roots_are_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project_file = tmp_path / "not-a-project"
    project_file.write_text("file\n", encoding="utf-8")

    with pytest.raises(EvaluationInfrastructureError) as fixtures:
        run_evaluation(
            project_root=root,
            fixtures_root=Path("missing-fixtures"),
            output_root=Path("o"),
            agent_runner=lambda _fixture, _workspace: pytest.fail("runner called"),
            model=MODEL,
            limits=LIMITS,
        )
    with pytest.raises(EvaluationInfrastructureError) as project:
        run_evaluation(
            project_root=project_file,
            agent_runner=lambda _fixture, _workspace: pytest.fail("runner called"),
            model=MODEL,
            limits=LIMITS,
        )

    assert fixtures.value.code == "PATH_INVALID"
    assert project.value.code == "PROJECT_ROOT_INVALID"
