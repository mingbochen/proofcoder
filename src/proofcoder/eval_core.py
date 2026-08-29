"""Offline evaluation execution, evidence scoring, and deterministic aggregation."""

from __future__ import annotations

import hashlib
import math
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from proofcoder.errors import ProofCoderError
from proofcoder.eval_fixtures import (
    EvalFixture,
    EvalFixtureError,
    FixtureCategory,
    materialize_fixture,
)
from proofcoder.protocol import CompletionStatus, RunResult, TerminationReason
from proofcoder.tools.base import ToolResult
from proofcoder.tools.command import create_run_command_tool

DEFAULT_EVALUATION_TIMEOUT_SECONDS = 60
_HASH_CHUNK_BYTES = 64 * 1024
_IGNORED_ROOT_DIRECTORY = ".proofcoder"
_COMMAND_TIMEOUT_CODE = "COMMAND_TIMEOUT"
_AUDIT_WRITE_FAILURE = "AUDIT_WRITE_FAILED"

AgentRunner = Callable[[EvalFixture, Path], RunResult]


class EvaluationFailureReason(StrEnum):
    """Stable evidence-based reasons why one attempt did not succeed."""

    MATERIALIZATION_ERROR = "materialization_error"
    INITIAL_VALIDATION_ERROR = "initial_validation_error"
    INITIAL_VALIDATION_TIMEOUT = "initial_validation_timeout"
    INITIAL_EXIT_CODE_MISMATCH = "initial_exit_code_mismatch"
    INITIAL_OUTPUT_MISMATCH = "initial_output_mismatch"
    RUNNER_ERROR = "runner_error"
    SNAPSHOT_ERROR = "snapshot_error"
    COMPLETION_NOT_VERIFIED = "completion_not_verified"
    FINAL_VALIDATION_ERROR = "final_validation_error"
    FINAL_VALIDATION_TIMEOUT = "final_validation_timeout"
    FINAL_VALIDATION_FAILED = "final_validation_failed"
    MISSING_REQUIRED_FILES = "missing_required_files"
    UNEXPECTED_FILES = "unexpected_files"


class WorkspaceSnapshotError(ProofCoderError):
    """A stable, non-sensitive workspace snapshot failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FileDigest:
    """One workspace-relative file identity without its content."""

    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Deterministically ordered hashes for regular workspace files."""

    files: tuple[FileDigest, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceChanges:
    """Added, modified, and deleted paths between two snapshots."""

    added_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()

    @property
    def changed_files(self) -> tuple[str, ...]:
        """Return every changed path once in deterministic order."""

        return tuple(sorted((*self.added_files, *self.modified_files, *self.deleted_files)))


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    """Bounded command evidence without stdout or stderr bodies."""

    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_truncated: bool
    stderr_truncated: bool
    error_code: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationAttemptResult:
    """One fixture attempt scored only from local evidence."""

    fixture_id: str
    category: FixtureCategory
    attempt_index: int
    success: bool
    failure_reasons: tuple[EvaluationFailureReason, ...]
    completion_status: CompletionStatus | None = None
    termination_reason: TerminationReason | None = None
    added_files: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    unexpected_files: tuple[str, ...] = ()
    missing_required_files: tuple[str, ...] = ()
    initial_validation: ValidationEvidence | None = None
    final_validation: ValidationEvidence | None = None
    model_call_count: int = 0
    tool_call_count: int = 0
    tool_error_count: int = 0
    api_attempt_count: int = 0
    api_retry_count: int = 0
    context_compaction_count: int = 0
    input_token_count: int = 0
    output_token_count: int = 0
    elapsed_seconds: float = 0.0
    run_id: str = ""
    trace_path: str | None = None
    trace_complete: bool = False


@dataclass(frozen=True, slots=True)
class AggregateMetrics:
    """Deterministic totals for one result group."""

    attempts: int
    successes: int
    success_rate: float
    model_calls: int
    tool_calls: int
    tool_errors: int
    api_attempts: int
    api_retries: int
    context_compactions: int
    input_tokens: int
    output_tokens: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class FixtureAggregate:
    fixture_id: str
    category: FixtureCategory
    metrics: AggregateMetrics


@dataclass(frozen=True, slots=True)
class CategoryAggregate:
    category: FixtureCategory
    metrics: AggregateMetrics


@dataclass(frozen=True, slots=True)
class EvaluationAggregate:
    """Per-fixture, per-category, and overall aggregate metrics."""

    fixtures: tuple[FixtureAggregate, ...]
    categories: tuple[CategoryAggregate, ...]
    overall: AggregateMetrics


def snapshot_workspace(workspace: Path) -> WorkspaceSnapshot:
    """Hash regular files without following links or retaining file bodies."""

    if workspace.is_symlink():
        raise WorkspaceSnapshotError("SNAPSHOT_SYMLINK", "workspace must not be a symlink")
    try:
        root = workspace.resolve(strict=True)
    except OSError:
        raise WorkspaceSnapshotError(
            "SNAPSHOT_ROOT_INVALID", "workspace could not be resolved safely"
        ) from None
    if not root.is_dir():
        raise WorkspaceSnapshotError(
            "SNAPSHOT_ROOT_INVALID", "workspace must be an existing directory"
        )

    files: list[FileDigest] = []

    def visit(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(
                directory.iterdir(), key=lambda path: (path.name.casefold(), path.name)
            )
        except OSError:
            raise WorkspaceSnapshotError(
                "SNAPSHOT_READ_ERROR", "workspace could not be listed safely"
            ) from None
        for entry in entries:
            relative = entry.name if not prefix else f"{prefix}/{entry.name}"
            if not prefix and entry.name.casefold() == _IGNORED_ROOT_DIRECTORY:
                continue
            if entry.is_symlink():
                raise WorkspaceSnapshotError(
                    "SNAPSHOT_SYMLINK", "workspace snapshots reject symbolic links"
                )
            try:
                resolved = entry.resolve(strict=True)
                resolved.relative_to(root)
                metadata = entry.stat(follow_symlinks=False)
            except (OSError, ValueError):
                raise WorkspaceSnapshotError(
                    "SNAPSHOT_OUTSIDE_WORKSPACE",
                    "workspace entry could not be proven to remain inside the workspace",
                ) from None
            if stat.S_ISDIR(metadata.st_mode):
                visit(entry, relative)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(FileDigest(relative, _sha256_file(entry)))
            else:
                raise WorkspaceSnapshotError(
                    "SNAPSHOT_SPECIAL_FILE", "workspace snapshots accept only files and directories"
                )

    visit(root, "")
    return WorkspaceSnapshot(tuple(files))


def compare_snapshots(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> WorkspaceChanges:
    """Compare two immutable snapshots without reading the workspace again."""

    before_files = _snapshot_mapping(before)
    after_files = _snapshot_mapping(after)
    before_paths = set(before_files)
    after_paths = set(after_files)
    return WorkspaceChanges(
        added_files=tuple(sorted(after_paths - before_paths)),
        modified_files=tuple(
            sorted(
                path
                for path in before_paths & after_paths
                if before_files[path] != after_files[path]
            )
        ),
        deleted_files=tuple(sorted(before_paths - after_paths)),
    )


def run_evaluation_attempt(
    fixture: EvalFixture,
    workspace: Path,
    attempt_index: int,
    agent_runner: AgentRunner,
    *,
    command_timeout_seconds: int = DEFAULT_EVALUATION_TIMEOUT_SECONDS,
    environ: Mapping[str, str] | None = None,
) -> EvaluationAttemptResult:
    """Materialize, run, independently verify, and score one fixture attempt."""

    if attempt_index < 1:
        raise ValueError("attempt_index must be positive")

    try:
        materialize_fixture(fixture, workspace)
    except EvalFixtureError:
        return _attempt_result(
            fixture,
            attempt_index,
            reasons=(EvaluationFailureReason.MATERIALIZATION_ERROR,),
            missing_required=fixture.required_modified_files,
        )

    command = create_run_command_tool(workspace, environ=environ)
    initial_validation, initial_output = _run_validation(
        fixture,
        command.execute(
            {
                "argv": list(fixture.validation.argv),
                "cwd": fixture.validation.cwd,
                "timeout_seconds": command_timeout_seconds,
            }
        ),
        command_timeout_seconds,
    )
    initial_reasons = _initial_validation_reasons(fixture, initial_validation, initial_output)
    if initial_reasons:
        return _attempt_result(
            fixture,
            attempt_index,
            reasons=initial_reasons,
            initial_validation=initial_validation,
            missing_required=fixture.required_modified_files,
        )

    try:
        before = snapshot_workspace(workspace)
    except WorkspaceSnapshotError:
        return _attempt_result(
            fixture,
            attempt_index,
            reasons=(EvaluationFailureReason.SNAPSHOT_ERROR,),
            initial_validation=initial_validation,
            missing_required=fixture.required_modified_files,
        )

    run_result: RunResult | None = None
    runner_failed = False
    try:
        candidate = agent_runner(fixture, workspace)
        if not isinstance(candidate, RunResult):
            runner_failed = True
        else:
            run_result = candidate
    except Exception:
        runner_failed = True

    try:
        after = snapshot_workspace(workspace)
        changes = compare_snapshots(before, after)
    except WorkspaceSnapshotError:
        reasons = [EvaluationFailureReason.SNAPSHOT_ERROR]
        if runner_failed:
            reasons.append(EvaluationFailureReason.RUNNER_ERROR)
        return _attempt_result(
            fixture,
            attempt_index,
            reasons=tuple(reasons),
            initial_validation=initial_validation,
            run_result=run_result,
            missing_required=fixture.required_modified_files,
        )

    missing_required, unexpected = _scope_differences(fixture, changes)
    if runner_failed:
        reasons = [EvaluationFailureReason.RUNNER_ERROR]
        _append_scope_reasons(reasons, missing_required, unexpected)
        return _attempt_result(
            fixture,
            attempt_index,
            reasons=tuple(reasons),
            changes=changes,
            initial_validation=initial_validation,
            run_result=run_result,
            missing_required=missing_required,
            unexpected=unexpected,
        )

    assert run_result is not None
    final_validation, _ = _run_validation(
        fixture,
        command.execute(
            {
                "argv": list(fixture.validation.argv),
                "cwd": fixture.validation.cwd,
                "timeout_seconds": command_timeout_seconds,
            }
        ),
        command_timeout_seconds,
    )
    reasons: list[EvaluationFailureReason] = []
    if run_result.completion_status is not CompletionStatus.COMPLETED_VERIFIED:
        reasons.append(EvaluationFailureReason.COMPLETION_NOT_VERIFIED)
    reasons.extend(_final_validation_reasons(fixture, final_validation))
    _append_scope_reasons(reasons, missing_required, unexpected)
    return _attempt_result(
        fixture,
        attempt_index,
        reasons=tuple(reasons),
        changes=changes,
        initial_validation=initial_validation,
        final_validation=final_validation,
        run_result=run_result,
        missing_required=missing_required,
        unexpected=unexpected,
    )


def aggregate_evaluation_results(
    results: Iterable[EvaluationAttemptResult],
) -> EvaluationAggregate:
    """Aggregate attempts independently of their input order."""

    ordered = tuple(
        sorted(
            results,
            key=lambda result: (
                result.fixture_id,
                result.category.value,
                result.attempt_index,
                result.run_id,
            ),
        )
    )
    fixture_groups: dict[str, list[EvaluationAttemptResult]] = {}
    fixture_categories: dict[str, FixtureCategory] = {}
    category_groups: dict[FixtureCategory, list[EvaluationAttemptResult]] = {}
    for result in ordered:
        known_category = fixture_categories.setdefault(result.fixture_id, result.category)
        if known_category is not result.category:
            raise ValueError("one fixture ID cannot belong to multiple categories")
        fixture_groups.setdefault(result.fixture_id, []).append(result)
        category_groups.setdefault(result.category, []).append(result)

    fixtures = tuple(
        FixtureAggregate(
            fixture_id=fixture_id,
            category=fixture_categories[fixture_id],
            metrics=_aggregate_metrics(group),
        )
        for fixture_id, group in sorted(fixture_groups.items())
    )
    categories = tuple(
        CategoryAggregate(category=category, metrics=_aggregate_metrics(group))
        for category, group in sorted(category_groups.items(), key=lambda item: item[0].value)
    )
    return EvaluationAggregate(
        fixtures=fixtures,
        categories=categories,
        overall=_aggregate_metrics(ordered),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError:
        raise WorkspaceSnapshotError(
            "SNAPSHOT_READ_ERROR", "workspace file could not be hashed safely"
        ) from None
    return digest.hexdigest()


def _snapshot_mapping(snapshot: WorkspaceSnapshot) -> dict[str, str]:
    mapping = {item.path: item.sha256 for item in snapshot.files}
    if len(mapping) != len(snapshot.files):
        raise ValueError("workspace snapshots must not contain duplicate paths")
    return mapping


def _run_validation(
    fixture: EvalFixture,
    result: ToolResult,
    timeout_seconds: int,
) -> tuple[ValidationEvidence, str]:
    requested_argv = fixture.validation.argv
    requested_cwd = fixture.validation.cwd
    error_code = None if result.error is None else result.error.code
    data = result.data
    if data is None:
        return (
            ValidationEvidence(
                argv=requested_argv,
                cwd=requested_cwd,
                timeout_seconds=timeout_seconds,
                exit_code=None,
                timed_out=False,
                duration_ms=result.meta.duration_ms,
                stdout_truncated=False,
                stderr_truncated=False,
                error_code=error_code or "INVALID_COMMAND_RESULT",
                warnings=result.meta.warnings,
            ),
            "",
        )

    argv = data.get("argv")
    cwd = data.get("cwd")
    exit_code = data.get("exit_code")
    timed_out = data.get("timed_out")
    stdout = data.get("stdout")
    stderr = data.get("stderr")
    stdout_truncated = data.get("stdout_truncated")
    stderr_truncated = data.get("stderr_truncated")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) for item in argv)
        or not isinstance(cwd, str)
        or (exit_code is not None and type(exit_code) is not int)
        or type(timed_out) is not bool
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or type(stdout_truncated) is not bool
        or type(stderr_truncated) is not bool
    ):
        return (
            ValidationEvidence(
                argv=requested_argv,
                cwd=requested_cwd,
                timeout_seconds=timeout_seconds,
                exit_code=None,
                timed_out=False,
                duration_ms=result.meta.duration_ms,
                stdout_truncated=False,
                stderr_truncated=False,
                error_code="INVALID_COMMAND_RESULT",
                warnings=result.meta.warnings,
            ),
            "",
        )
    return (
        ValidationEvidence(
            argv=tuple(argv),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=result.meta.duration_ms,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            error_code=error_code,
            warnings=result.meta.warnings,
        ),
        stdout + stderr,
    )


def _initial_validation_reasons(
    fixture: EvalFixture,
    evidence: ValidationEvidence,
    output: str,
) -> tuple[EvaluationFailureReason, ...]:
    if evidence.timed_out or evidence.error_code == _COMMAND_TIMEOUT_CODE:
        return (EvaluationFailureReason.INITIAL_VALIDATION_TIMEOUT,)
    if _validation_infrastructure_failed(evidence):
        return (EvaluationFailureReason.INITIAL_VALIDATION_ERROR,)
    reasons: list[EvaluationFailureReason] = []
    if evidence.exit_code != fixture.validation.initial_exit_code:
        reasons.append(EvaluationFailureReason.INITIAL_EXIT_CODE_MISMATCH)
    if fixture.validation.initial_output_contains not in output:
        reasons.append(EvaluationFailureReason.INITIAL_OUTPUT_MISMATCH)
    return tuple(reasons)


def _final_validation_reasons(
    fixture: EvalFixture,
    evidence: ValidationEvidence,
) -> tuple[EvaluationFailureReason, ...]:
    if evidence.timed_out or evidence.error_code == _COMMAND_TIMEOUT_CODE:
        return (EvaluationFailureReason.FINAL_VALIDATION_TIMEOUT,)
    if _validation_infrastructure_failed(evidence):
        return (EvaluationFailureReason.FINAL_VALIDATION_ERROR,)
    if evidence.exit_code != fixture.validation.success_exit_code:
        return (EvaluationFailureReason.FINAL_VALIDATION_FAILED,)
    return ()


def _validation_infrastructure_failed(evidence: ValidationEvidence) -> bool:
    return evidence.error_code is not None or any(
        warning.startswith(_AUDIT_WRITE_FAILURE) for warning in evidence.warnings
    )


def _scope_differences(
    fixture: EvalFixture,
    changes: WorkspaceChanges,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    changed = set(changes.changed_files)
    missing = tuple(sorted(set(fixture.required_modified_files) - changed))
    unexpected = tuple(sorted(changed - set(fixture.allowed_modified_files)))
    return missing, unexpected


def _append_scope_reasons(
    reasons: list[EvaluationFailureReason],
    missing_required: tuple[str, ...],
    unexpected: tuple[str, ...],
) -> None:
    if missing_required:
        reasons.append(EvaluationFailureReason.MISSING_REQUIRED_FILES)
    if unexpected:
        reasons.append(EvaluationFailureReason.UNEXPECTED_FILES)


def _attempt_result(
    fixture: EvalFixture,
    attempt_index: int,
    *,
    reasons: tuple[EvaluationFailureReason, ...],
    changes: WorkspaceChanges | None = None,
    initial_validation: ValidationEvidence | None = None,
    final_validation: ValidationEvidence | None = None,
    run_result: RunResult | None = None,
    missing_required: tuple[str, ...] = (),
    unexpected: tuple[str, ...] = (),
) -> EvaluationAttemptResult:
    observed_changes = WorkspaceChanges() if changes is None else changes
    ordered_reasons = tuple(sorted(set(reasons), key=lambda reason: reason.value))
    return EvaluationAttemptResult(
        fixture_id=fixture.fixture_id,
        category=fixture.category,
        attempt_index=attempt_index,
        success=not ordered_reasons,
        failure_reasons=ordered_reasons,
        completion_status=None if run_result is None else run_result.completion_status,
        termination_reason=None if run_result is None else run_result.termination_reason,
        added_files=observed_changes.added_files,
        modified_files=observed_changes.modified_files,
        deleted_files=observed_changes.deleted_files,
        changed_files=observed_changes.changed_files,
        unexpected_files=unexpected,
        missing_required_files=missing_required,
        initial_validation=initial_validation,
        final_validation=final_validation,
        model_call_count=0 if run_result is None else run_result.model_call_count,
        tool_call_count=0 if run_result is None else run_result.tool_call_count,
        tool_error_count=0 if run_result is None else run_result.tool_error_count,
        api_attempt_count=0 if run_result is None else run_result.api_attempt_count,
        api_retry_count=0 if run_result is None else run_result.api_retry_count,
        context_compaction_count=(0 if run_result is None else run_result.context_compaction_count),
        input_token_count=0 if run_result is None else run_result.input_token_count,
        output_token_count=0 if run_result is None else run_result.output_token_count,
        elapsed_seconds=0.0 if run_result is None else run_result.elapsed_seconds,
        run_id="" if run_result is None else run_result.run_id,
        trace_path=None if run_result is None else run_result.trace_path,
        trace_complete=False if run_result is None else run_result.trace_complete,
    )


def _aggregate_metrics(results: Iterable[EvaluationAttemptResult]) -> AggregateMetrics:
    items = tuple(results)
    attempts = len(items)
    successes = sum(result.success for result in items)
    return AggregateMetrics(
        attempts=attempts,
        successes=successes,
        success_rate=0.0 if attempts == 0 else successes / attempts,
        model_calls=sum(result.model_call_count for result in items),
        tool_calls=sum(result.tool_call_count for result in items),
        tool_errors=sum(result.tool_error_count for result in items),
        api_attempts=sum(result.api_attempt_count for result in items),
        api_retries=sum(result.api_retry_count for result in items),
        context_compactions=sum(result.context_compaction_count for result in items),
        input_tokens=sum(result.input_token_count for result in items),
        output_tokens=sum(result.output_token_count for result in items),
        elapsed_seconds=math.fsum(sorted(result.elapsed_seconds for result in items)),
    )
