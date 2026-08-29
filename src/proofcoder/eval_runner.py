"""Deterministic repeated evaluation orchestration and durable local results."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from proofcoder.agent_runtime import (
    AgentRunLimits,
    build_agent_loop,
    create_agent_runtime_resources,
    emit_setup_termination,
    setup_failure_result,
)
from proofcoder.config import ProofCoderConfig
from proofcoder.errors import ProofCoderError
from proofcoder.eval_core import (
    AgentRunner,
    AggregateMetrics,
    EvaluationAggregate,
    EvaluationAttemptInfrastructureError,
    EvaluationAttemptResult,
    EvaluationFailureReason,
    ValidationEvidence,
    aggregate_evaluation_results,
    run_evaluation_attempt,
)
from proofcoder.eval_fixtures import EvalFixture, EvalFixtureError, load_fixtures
from proofcoder.events import new_run_id
from proofcoder.llm.base import LLMClient
from proofcoder.protocol import RunResult, TerminationReason
from proofcoder.safety.secrets import sensitive_environment_values
from proofcoder.safety.writes import (
    commit_new_file,
    commit_replacement,
    discard_temporary_file,
    stage_temporary_file,
)
from proofcoder.tools.base import ToolDefinition
from proofcoder.tools.command import create_run_command_tool
from proofcoder.trace import TracePathError, read_trace, validate_run_id

EVALUATION_SCHEMA_VERSION = 1
DEFAULT_REPEAT = 3
MIN_REPEAT = 1
MAX_REPEAT = 10
DEFAULT_FIXTURES_ROOT = Path("evals/fixtures")
DEFAULT_OUTPUT_ROOT = Path(".proofcoder/evals")
_ATTEMPTS_FILENAME = "attempts.jsonl"
_METADATA_FILENAME = "metadata.json"
_SUMMARY_FILENAME = "summary.json"
_INFRASTRUCTURE_REASONS = frozenset(
    {
        EvaluationFailureReason.MATERIALIZATION_ERROR,
        EvaluationFailureReason.INITIAL_VALIDATION_ERROR,
        EvaluationFailureReason.INITIAL_VALIDATION_TIMEOUT,
        EvaluationFailureReason.INITIAL_EXIT_CODE_MISMATCH,
        EvaluationFailureReason.INITIAL_OUTPUT_MISMATCH,
        EvaluationFailureReason.SNAPSHOT_ERROR,
        EvaluationFailureReason.FINAL_VALIDATION_ERROR,
    }
)

EvaluationClientFactory = Callable[[ProofCoderConfig], LLMClient]


class EvaluationStatus(StrEnum):
    """Persisted lifecycle state for one evaluation session."""

    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class EvaluationProgressKind(StrEnum):
    """Presentation-neutral progress notifications from the orchestrator."""

    STARTED = "started"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    FINISHED = "finished"


class EvaluationInfrastructureError(ProofCoderError):
    """A stable setup or persistence error that prevents trustworthy evaluation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvaluationModelInfo:
    """Non-secret model configuration persisted with evaluation metadata."""

    name: str
    base_url: str
    reasoning_effort: str


@dataclass(frozen=True, slots=True)
class EvaluationProgress:
    """One local progress notification safe for terminal rendering."""

    kind: EvaluationProgressKind
    eval_id: str
    fixture_id: str | None = None
    attempt_index: int | None = None
    repeat: int | None = None
    result: EvaluationAttemptResult | None = None
    status: EvaluationStatus | None = None
    aggregate: EvaluationAggregate | None = None
    evaluation_directory: Path | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationSessionResult:
    """Completed orchestration facts returned independently of the CLI."""

    eval_id: str
    status: EvaluationStatus
    evaluation_directory: Path
    attempts: tuple[EvaluationAttemptResult, ...]
    aggregate: EvaluationAggregate
    failure_code: str | None = None

    @property
    def exit_code(self) -> int:
        """Map the persisted evaluation outcome to the public CLI contract."""

        if self.status is EvaluationStatus.INTERRUPTED:
            return 130
        if self.status is EvaluationStatus.FAILED:
            return 2
        return 0 if all(attempt.success for attempt in self.attempts) else 1


@dataclass(frozen=True, slots=True)
class _GitState:
    revision: str | None
    dirty: bool | None
    warnings: tuple[str, ...]


@dataclass(slots=True)
class _SessionWriter:
    evaluation_directory: Path
    attempts_stream: BinaryIO

    @classmethod
    def create(
        cls,
        evaluation_directory: Path,
        *,
        metadata: Mapping[str, object],
        initial_summary: Mapping[str, object],
    ) -> _SessionWriter:
        """Create all top-level result files without overwriting existing evidence."""

        stream: BinaryIO | None = None
        try:
            _write_new_json(evaluation_directory / _METADATA_FILENAME, metadata)
            stream = (evaluation_directory / _ATTEMPTS_FILENAME).open("xb")
            writer = cls(evaluation_directory=evaluation_directory, attempts_stream=stream)
            writer.write_summary(initial_summary)
            return writer
        except EvaluationInfrastructureError:
            if stream is not None:
                stream.close()
            raise
        except OSError:
            if stream is not None:
                stream.close()
            raise EvaluationInfrastructureError(
                "RESULT_WRITE_FAILED",
                "evaluation result files could not be created safely",
            ) from None

    def append_attempt(self, payload: Mapping[str, object]) -> None:
        """Append, flush, and sync one complete deterministic JSONL record."""

        encoded = _canonical_json_line(payload)
        try:
            written = self.attempts_stream.write(encoded)
            if written != len(encoded):
                raise OSError
            self.attempts_stream.flush()
            os.fsync(self.attempts_stream.fileno())
        except OSError:
            raise EvaluationInfrastructureError(
                "RESULT_WRITE_FAILED",
                "the completed evaluation attempt could not be persisted",
            ) from None

    def write_summary(self, payload: Mapping[str, object]) -> None:
        """Atomically replace summary.json with a complete current snapshot."""

        try:
            _replace_json(self.evaluation_directory / _SUMMARY_FILENAME, payload)
        except OSError:
            raise EvaluationInfrastructureError(
                "RESULT_WRITE_FAILED",
                "the evaluation summary could not be updated atomically",
            ) from None

    def close(self) -> None:
        """Close the append stream and surface a final persistence failure."""

        try:
            self.attempts_stream.close()
        except OSError:
            raise EvaluationInfrastructureError(
                "RESULT_WRITE_FAILED",
                "the evaluation attempt log could not be closed safely",
            ) from None


def create_evaluation_agent_runner(
    *,
    config: ProofCoderConfig,
    limits: AgentRunLimits,
    environ: Mapping[str, str] | None,
    client_factory: EvaluationClientFactory,
) -> AgentRunner:
    """Create the production runner while keeping the orchestrator provider-independent."""

    sensitive_values = sensitive_environment_values(environ)

    def run_agent(fixture: EvalFixture, workspace: Path) -> RunResult:
        try:
            resources = create_agent_runtime_resources(
                workspace,
                environ=environ,
                sensitive_values=sensitive_values,
            )
        except (OSError, TracePathError, ValueError) as error:
            code = getattr(error, "code", "AGENT_SETUP_FAILED")
            raise EvaluationAttemptInfrastructureError(
                str(code), "evaluation agent resources could not be created safely"
            ) from None

        result: RunResult
        try:
            try:
                client = client_factory(config)
            except KeyboardInterrupt:
                emit_setup_termination(
                    task=fixture.task,
                    resources=resources,
                    termination_reason=TerminationReason.INTERRUPTED,
                    sensitive_values=sensitive_values,
                )
                result = setup_failure_result(
                    task=fixture.task,
                    resources=resources,
                    termination_reason=TerminationReason.INTERRUPTED,
                )
            except ProofCoderError:
                emit_setup_termination(
                    task=fixture.task,
                    resources=resources,
                    termination_reason=TerminationReason.API_ERROR,
                    sensitive_values=sensitive_values,
                )
                result = setup_failure_result(
                    task=fixture.task,
                    resources=resources,
                    termination_reason=TerminationReason.API_ERROR,
                )
            else:
                result = build_agent_loop(
                    client=client,
                    resources=resources,
                    limits=limits,
                    sensitive_values=sensitive_values,
                ).run(fixture.task)
        finally:
            resources.close()
        return replace(
            result,
            trace_complete=result.trace_complete and resources.recorder.trace_complete,
        )

    return run_agent


def run_evaluation(
    *,
    project_root: Path,
    fixtures_root: Path = DEFAULT_FIXTURES_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    fixture_ids: tuple[str, ...] = (),
    repeat: int = DEFAULT_REPEAT,
    agent_runner: AgentRunner,
    model: EvaluationModelInfo,
    limits: AgentRunLimits,
    environ: Mapping[str, str] | None = None,
    command_timeout_seconds: int = 60,
    eval_id_factory: Callable[[], str] = new_run_id,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    on_progress: Callable[[EvaluationProgress], None] | None = None,
) -> EvaluationSessionResult:
    """Run selected fixtures in stable order and persist every completed attempt."""

    if not MIN_REPEAT <= repeat <= MAX_REPEAT:
        raise EvaluationInfrastructureError(
            "INVALID_REPEAT", f"repeat must be between {MIN_REPEAT} and {MAX_REPEAT}"
        )
    root = _existing_project_root(project_root)
    fixture_directory = _resolve_project_directory(root, fixtures_root, label="fixture root")
    try:
        fixtures = _select_fixtures(load_fixtures(fixture_directory), fixture_ids)
    except EvalFixtureError as error:
        raise EvaluationInfrastructureError(error.code, str(error)) from None

    output_directory = _resolve_project_output(root, output_root)
    _ensure_directory_tree(root, output_directory)
    eval_id = eval_id_factory()
    try:
        validate_run_id(eval_id)
    except TracePathError:
        raise EvaluationInfrastructureError(
            "INVALID_EVAL_ID", "the local eval ID generator returned an invalid identifier"
        ) from None
    evaluation_directory = output_directory / eval_id
    _create_evaluation_directory(output_directory, evaluation_directory)

    started_at = _rfc3339(clock())
    git_state = _read_git_state(root, environ=environ)
    expected_attempts = len(fixtures) * repeat
    results: list[EvaluationAttemptResult] = []
    metadata = _metadata_payload(
        eval_id=eval_id,
        started_at=started_at,
        model=model,
        repeat=repeat,
        fixtures=fixtures,
        limits=limits,
        command_timeout_seconds=command_timeout_seconds,
        git_state=git_state,
    )
    initial_aggregate = aggregate_evaluation_results(())
    initial_summary = _summary_payload(
        eval_id=eval_id,
        status=EvaluationStatus.RUNNING,
        started_at=started_at,
        completed_at=None,
        expected_attempts=expected_attempts,
        selected_fixtures=fixtures,
        model_name=model.name,
        aggregate=initial_aggregate,
    )
    writer = _SessionWriter.create(
        evaluation_directory,
        metadata=metadata,
        initial_summary=initial_summary,
    )
    _notify(
        on_progress,
        EvaluationProgress(
            kind=EvaluationProgressKind.STARTED,
            eval_id=eval_id,
            evaluation_directory=evaluation_directory,
        ),
    )

    terminal_status = EvaluationStatus.COMPLETED
    failure_code: str | None = None
    try:
        sequence = 0
        stop = False
        for fixture in fixtures:
            for attempt_index in range(1, repeat + 1):
                sequence += 1
                attempt_directory = evaluation_directory / f"{sequence:03d}"
                workspace = attempt_directory / "w"
                try:
                    _ensure_attempt_parent(evaluation_directory, attempt_directory)
                    _notify(
                        on_progress,
                        EvaluationProgress(
                            kind=EvaluationProgressKind.ATTEMPT_STARTED,
                            eval_id=eval_id,
                            fixture_id=fixture.fixture_id,
                            attempt_index=attempt_index,
                            repeat=repeat,
                        ),
                    )
                    attempt = run_evaluation_attempt(
                        fixture,
                        workspace,
                        attempt_index,
                        agent_runner,
                        command_timeout_seconds=command_timeout_seconds,
                        environ=environ,
                    )
                    attempt = _normalize_trace(attempt, workspace, evaluation_directory)
                    writer.append_attempt(
                        _attempt_payload(
                            attempt,
                            sequence=sequence,
                            workspace=workspace.relative_to(evaluation_directory).as_posix(),
                        )
                    )
                    results.append(attempt)
                    aggregate = aggregate_evaluation_results(results)
                    if attempt.termination_reason is TerminationReason.INTERRUPTED:
                        terminal_status = EvaluationStatus.INTERRUPTED
                        stop = True
                    elif any(
                        reason in _INFRASTRUCTURE_REASONS for reason in attempt.failure_reasons
                    ):
                        terminal_status = EvaluationStatus.FAILED
                        failure_code = "ATTEMPT_INFRASTRUCTURE_FAILED"
                        stop = True
                    writer.write_summary(
                        _summary_payload(
                            eval_id=eval_id,
                            status=terminal_status if stop else EvaluationStatus.RUNNING,
                            started_at=started_at,
                            completed_at=_rfc3339(clock()) if stop else None,
                            expected_attempts=expected_attempts,
                            selected_fixtures=fixtures,
                            model_name=model.name,
                            aggregate=aggregate,
                            failure_code=failure_code,
                        )
                    )
                    _notify(
                        on_progress,
                        EvaluationProgress(
                            kind=EvaluationProgressKind.ATTEMPT_FINISHED,
                            eval_id=eval_id,
                            fixture_id=fixture.fixture_id,
                            attempt_index=attempt_index,
                            repeat=repeat,
                            result=attempt,
                        ),
                    )
                except KeyboardInterrupt:
                    terminal_status = EvaluationStatus.INTERRUPTED
                    stop = True
                    _best_effort_terminal_summary(
                        writer,
                        eval_id=eval_id,
                        status=terminal_status,
                        started_at=started_at,
                        completed_at=_rfc3339(clock()),
                        expected_attempts=expected_attempts,
                        selected_fixtures=fixtures,
                        model_name=model.name,
                        results=results,
                    )
                except EvaluationAttemptInfrastructureError as error:
                    terminal_status = EvaluationStatus.FAILED
                    failure_code = error.code
                    stop = True
                    _best_effort_terminal_summary(
                        writer,
                        eval_id=eval_id,
                        status=terminal_status,
                        started_at=started_at,
                        completed_at=_rfc3339(clock()),
                        expected_attempts=expected_attempts,
                        selected_fixtures=fixtures,
                        model_name=model.name,
                        results=results,
                        failure_code=failure_code,
                    )
                except EvaluationInfrastructureError as error:
                    terminal_status = EvaluationStatus.FAILED
                    failure_code = error.code
                    stop = True
                    _best_effort_terminal_summary(
                        writer,
                        eval_id=eval_id,
                        status=terminal_status,
                        started_at=started_at,
                        completed_at=_rfc3339(clock()),
                        expected_attempts=expected_attempts,
                        selected_fixtures=fixtures,
                        model_name=model.name,
                        results=results,
                        failure_code=failure_code,
                    )
                except OSError:
                    terminal_status = EvaluationStatus.FAILED
                    failure_code = "EVALUATION_DIRECTORY_FAILED"
                    stop = True
                    _best_effort_terminal_summary(
                        writer,
                        eval_id=eval_id,
                        status=terminal_status,
                        started_at=started_at,
                        completed_at=_rfc3339(clock()),
                        expected_attempts=expected_attempts,
                        selected_fixtures=fixtures,
                        model_name=model.name,
                        results=results,
                        failure_code=failure_code,
                    )
                if stop:
                    break
            if stop:
                break

        aggregate = aggregate_evaluation_results(results)
        if terminal_status is EvaluationStatus.COMPLETED:
            writer.write_summary(
                _summary_payload(
                    eval_id=eval_id,
                    status=terminal_status,
                    started_at=started_at,
                    completed_at=_rfc3339(clock()),
                    expected_attempts=expected_attempts,
                    selected_fixtures=fixtures,
                    model_name=model.name,
                    aggregate=aggregate,
                )
            )
    except EvaluationInfrastructureError as error:
        terminal_status = EvaluationStatus.FAILED
        failure_code = error.code
        aggregate = aggregate_evaluation_results(results)
    finally:
        try:
            writer.close()
        except EvaluationInfrastructureError as error:
            terminal_status = EvaluationStatus.FAILED
            failure_code = error.code
            _best_effort_terminal_summary(
                writer,
                eval_id=eval_id,
                status=terminal_status,
                started_at=started_at,
                completed_at=_rfc3339(clock()),
                expected_attempts=expected_attempts,
                selected_fixtures=fixtures,
                model_name=model.name,
                results=results,
                failure_code=failure_code,
            )

    session = EvaluationSessionResult(
        eval_id=eval_id,
        status=terminal_status,
        evaluation_directory=evaluation_directory,
        attempts=tuple(results),
        aggregate=aggregate,
        failure_code=failure_code,
    )
    _notify(
        on_progress,
        EvaluationProgress(
            kind=EvaluationProgressKind.FINISHED,
            eval_id=eval_id,
            status=terminal_status,
            aggregate=aggregate,
            evaluation_directory=evaluation_directory,
            failure_code=failure_code,
        ),
    )
    return session


def _existing_project_root(project_root: Path) -> Path:
    try:
        root = project_root.resolve(strict=True)
    except OSError:
        raise EvaluationInfrastructureError(
            "PROJECT_ROOT_INVALID", "the current project root does not exist"
        ) from None
    if not root.is_dir():
        raise EvaluationInfrastructureError(
            "PROJECT_ROOT_INVALID", "the current project root is not a directory"
        )
    return root


def _resolve_project_directory(root: Path, argument: Path, *, label: str) -> Path:
    candidate = _resolve_project_path(root, argument, label=label)
    if not candidate.exists() or not candidate.is_dir():
        raise EvaluationInfrastructureError(
            "PATH_INVALID", f"{label} must be an existing directory inside the project"
        )
    return candidate


def _resolve_project_output(root: Path, argument: Path) -> Path:
    candidate = _resolve_project_path(root, argument, label="output root")
    if candidate.exists() and not candidate.is_dir():
        raise EvaluationInfrastructureError(
            "OUTPUT_ROOT_INVALID", "output root must be a directory inside the project"
        )
    return candidate


def _resolve_project_path(root: Path, argument: Path, *, label: str) -> Path:
    raw = argument if argument.is_absolute() else root / argument
    lexical = Path(os.path.abspath(raw))
    try:
        relative = lexical.relative_to(root)
    except ValueError:
        raise EvaluationInfrastructureError(
            "PATH_OUTSIDE_PROJECT", f"{label} must remain inside the current project"
        ) from None
    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and _is_link_like(current):
            raise EvaluationInfrastructureError(
                "PATH_SYMLINK", f"{label} must not contain symbolic-link components"
            )
    try:
        resolved = lexical.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise EvaluationInfrastructureError(
            "PATH_OUTSIDE_PROJECT", f"{label} could not be proven to remain in the project"
        ) from None
    return resolved


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _ensure_directory_tree(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError:
        raise EvaluationInfrastructureError(
            "PATH_OUTSIDE_PROJECT", "output root must remain inside the current project"
        ) from None
    current = root
    try:
        for part in relative.parts:
            current /= part
            if os.path.lexists(current):
                if _is_link_like(current) or not current.is_dir():
                    raise EvaluationInfrastructureError(
                        "OUTPUT_ROOT_INVALID",
                        "output root contains an unsafe existing path component",
                    )
            else:
                current.mkdir(mode=0o700)
            current.resolve(strict=True).relative_to(root)
    except EvaluationInfrastructureError:
        raise
    except (OSError, ValueError):
        raise EvaluationInfrastructureError(
            "OUTPUT_ROOT_INVALID", "output root could not be created safely"
        ) from None


def _create_evaluation_directory(output_root: Path, evaluation_directory: Path) -> None:
    try:
        evaluation_directory.relative_to(output_root)
        evaluation_directory.mkdir(mode=0o700)
        evaluation_directory.resolve(strict=True).relative_to(output_root.resolve(strict=True))
    except (FileExistsError, OSError, ValueError):
        raise EvaluationInfrastructureError(
            "EVAL_DIRECTORY_UNAVAILABLE",
            "a new isolated evaluation directory could not be created",
        ) from None


def _ensure_attempt_parent(evaluation_directory: Path, attempt_directory: Path) -> None:
    try:
        attempt_directory.relative_to(evaluation_directory)
        current = evaluation_directory
        relative = attempt_directory.relative_to(evaluation_directory)
        for part in relative.parts:
            current /= part
            if os.path.lexists(current):
                if _is_link_like(current) or not current.is_dir():
                    raise OSError
            else:
                current.mkdir(mode=0o700)
            current.resolve(strict=True).relative_to(evaluation_directory.resolve(strict=True))
    except (OSError, ValueError):
        raise EvaluationInfrastructureError(
            "EVALUATION_DIRECTORY_FAILED",
            "an isolated attempt directory could not be created safely",
        ) from None


def _select_fixtures(
    available: tuple[EvalFixture, ...], requested: tuple[str, ...]
) -> tuple[EvalFixture, ...]:
    if len(set(requested)) != len(requested):
        raise EvaluationInfrastructureError(
            "DUPLICATE_FIXTURE", "fixture IDs may be specified only once"
        )
    by_id = {fixture.fixture_id: fixture for fixture in available}
    unknown = sorted(set(requested) - set(by_id))
    if unknown:
        raise EvaluationInfrastructureError("UNKNOWN_FIXTURE", f"unknown fixture ID: {unknown[0]}")
    selected = available if not requested else tuple(by_id[item] for item in requested)
    return tuple(sorted(selected, key=lambda fixture: fixture.fixture_id))


def _read_git_state(project_root: Path, *, environ: Mapping[str, str] | None) -> _GitState:
    command = create_run_command_tool(project_root, environ=environ)
    revision = _git_output(command, ["git", "rev-parse", "HEAD"])
    status = _git_output(
        command,
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
    )
    warnings: list[str] = []
    if revision is None:
        warnings.append("GIT_REVISION_UNAVAILABLE")
    if status is None:
        warnings.append("GIT_DIRTY_UNAVAILABLE")
    normalized_revision = revision.strip() if revision is not None else None
    if normalized_revision is not None and (
        len(normalized_revision) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in normalized_revision)
    ):
        normalized_revision = None
        if "GIT_REVISION_UNAVAILABLE" not in warnings:
            warnings.append("GIT_REVISION_UNAVAILABLE")
    return _GitState(
        revision=normalized_revision,
        dirty=None if status is None else bool(status.strip()),
        warnings=tuple(warnings),
    )


def _git_output(command: ToolDefinition, argv: list[str]) -> str | None:
    result = command.execute({"argv": argv, "cwd": ".", "timeout_seconds": 30})
    data = result.data
    if (
        not result.ok
        or data is None
        or data.get("exit_code") != 0
        or data.get("timed_out") is not False
        or not isinstance(data.get("stdout"), str)
        or data.get("stdout_truncated") is not False
        or any(warning.startswith("AUDIT_WRITE_FAILED") for warning in result.meta.warnings)
    ):
        return None
    return str(data["stdout"])


def _metadata_payload(
    *,
    eval_id: str,
    started_at: str,
    model: EvaluationModelInfo,
    repeat: int,
    fixtures: tuple[EvalFixture, ...],
    limits: AgentRunLimits,
    command_timeout_seconds: int,
    git_state: _GitState,
) -> dict[str, object]:
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "eval_id": eval_id,
        "started_at": started_at,
        "model": {
            "name": model.name,
            "base_url": model.base_url,
            "reasoning_effort": model.reasoning_effort,
        },
        "repeat": repeat,
        "fixtures": [fixture.fixture_id for fixture in fixtures],
        "limits": {
            "max_steps": limits.max_steps,
            "max_seconds": limits.max_seconds,
            "context_budget_bytes": limits.context_budget_bytes,
            "max_consecutive_failures": limits.max_consecutive_failures,
            "max_api_attempts": limits.max_api_attempts,
            "validation_timeout_seconds": command_timeout_seconds,
        },
        "code": {"revision": git_state.revision, "dirty": git_state.dirty},
        "warnings": list(git_state.warnings),
    }


def _attempt_payload(
    result: EvaluationAttemptResult, *, sequence: int, workspace: str
) -> dict[str, object]:
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "sequence": sequence,
        "fixture_id": result.fixture_id,
        "category": result.category.value,
        "attempt": result.attempt_index,
        "success": result.success,
        "failure_reasons": [reason.value for reason in result.failure_reasons],
        "completion_status": (
            None if result.completion_status is None else result.completion_status.value
        ),
        "termination_reason": (
            None if result.termination_reason is None else result.termination_reason.value
        ),
        "files": {
            "added": list(result.added_files),
            "modified": list(result.modified_files),
            "deleted": list(result.deleted_files),
            "changed": list(result.changed_files),
            "ignored_runtime": list(result.ignored_runtime_files),
            "unexpected": list(result.unexpected_files),
            "missing_required": list(result.missing_required_files),
        },
        "validation": {
            "initial": _validation_payload(result.initial_validation),
            "final": _validation_payload(result.final_validation),
        },
        "statistics": {
            "model_calls": result.model_call_count,
            "tool_calls": result.tool_call_count,
            "tool_errors": result.tool_error_count,
            "api_attempts": result.api_attempt_count,
            "api_retries": result.api_retry_count,
            "context_compactions": result.context_compaction_count,
            "input_tokens": result.input_token_count,
            "output_tokens": result.output_token_count,
            "elapsed_seconds": _rounded_seconds(result.elapsed_seconds),
        },
        "run_id": result.run_id or None,
        "workspace": workspace,
        "trace_path": result.trace_path,
        "trace_complete": result.trace_complete,
    }


def _validation_payload(evidence: ValidationEvidence | None) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "argv": list(evidence.argv),
        "cwd": evidence.cwd,
        "timeout_seconds": evidence.timeout_seconds,
        "exit_code": evidence.exit_code,
        "timed_out": evidence.timed_out,
        "duration_ms": evidence.duration_ms,
        "stdout_truncated": evidence.stdout_truncated,
        "stderr_truncated": evidence.stderr_truncated,
        "error_code": evidence.error_code,
        "warnings": list(evidence.warnings),
    }


def _normalize_trace(
    result: EvaluationAttemptResult,
    workspace: Path,
    evaluation_directory: Path,
) -> EvaluationAttemptResult:
    if result.termination_reason is None:
        return result
    valid = False
    relative_trace: str | None = None
    if result.run_id and result.trace_path and result.trace_complete:
        try:
            trace = read_trace(workspace, result.run_id)
            claimed = PurePosixPath(result.trace_path)
            if (
                not claimed.is_absolute()
                and "\\" not in result.trace_path
                and all(part not in {"", ".", ".."} for part in claimed.parts)
                and trace.trace_complete
                and trace.trace_path == claimed.as_posix()
            ):
                absolute_trace = workspace.joinpath(*claimed.parts).resolve(strict=True)
                absolute_trace.relative_to(workspace.resolve(strict=True))
                relative_trace = absolute_trace.relative_to(evaluation_directory).as_posix()
                valid = absolute_trace.is_file()
        except (OSError, TracePathError, ValueError):
            valid = False
    if valid:
        return replace(result, trace_path=relative_trace, trace_complete=True)
    reasons = tuple(
        sorted(
            {*result.failure_reasons, EvaluationFailureReason.TRACE_INCOMPLETE},
            key=lambda reason: reason.value,
        )
    )
    return replace(
        result,
        success=False,
        failure_reasons=reasons,
        trace_path=None,
        trace_complete=False,
    )


def _summary_payload(
    *,
    eval_id: str,
    status: EvaluationStatus,
    started_at: str,
    completed_at: str | None,
    expected_attempts: int,
    selected_fixtures: tuple[EvalFixture, ...],
    model_name: str,
    aggregate: EvaluationAggregate,
    failure_code: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "eval_id": eval_id,
        "status": status.value,
        "started_at": started_at,
        "completed_at": completed_at,
        "expected_attempts": expected_attempts,
        "recorded_attempts": aggregate.overall.attempts,
        "selected_fixtures": [fixture.fixture_id for fixture in selected_fixtures],
        "model": model_name,
        "failure_code": failure_code,
        "fixtures": [
            {
                "fixture_id": item.fixture_id,
                "category": item.category.value,
                **_metrics_payload(item.metrics),
            }
            for item in aggregate.fixtures
        ],
        "categories": [
            {"category": item.category.value, **_metrics_payload(item.metrics)}
            for item in aggregate.categories
        ],
        "overall": _metrics_payload(aggregate.overall),
    }


def _metrics_payload(metrics: AggregateMetrics) -> dict[str, object]:
    return {
        "attempts": metrics.attempts,
        "successes": metrics.successes,
        "success_rate": metrics.success_rate,
        "failure_reason_counts": {
            reason.value: count for reason, count in metrics.failure_reason_counts
        },
        "model_calls": metrics.model_calls,
        "tool_calls": metrics.tool_calls,
        "tool_errors": metrics.tool_errors,
        "api_attempts": metrics.api_attempts,
        "api_retries": metrics.api_retries,
        "context_compactions": metrics.context_compactions,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "elapsed_seconds": _rounded_seconds(metrics.elapsed_seconds),
    }


def _rounded_seconds(value: float) -> float:
    rounded = round(value, 3)
    return 0.0 if rounded == 0 else rounded


def _best_effort_terminal_summary(
    writer: _SessionWriter,
    *,
    eval_id: str,
    status: EvaluationStatus,
    started_at: str,
    completed_at: str,
    expected_attempts: int,
    selected_fixtures: tuple[EvalFixture, ...],
    model_name: str,
    results: list[EvaluationAttemptResult],
    failure_code: str | None = None,
) -> None:
    try:
        writer.write_summary(
            _summary_payload(
                eval_id=eval_id,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                expected_attempts=expected_attempts,
                selected_fixtures=selected_fixtures,
                model_name=model_name,
                aggregate=aggregate_evaluation_results(results),
                failure_code=failure_code,
            )
        )
    except EvaluationInfrastructureError:
        return


def _write_new_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(path, _canonical_json_line(payload), mode=0o600)
        commit_new_file(temporary, path)
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)


def _replace_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(path, _canonical_json_line(payload), mode=0o600)
        commit_replacement(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)


def _canonical_json_line(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _notify(
    callback: Callable[[EvaluationProgress], None] | None,
    progress: EvaluationProgress,
) -> None:
    if callback is not None:
        callback(progress)
