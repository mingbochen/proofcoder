"""Command-line interface for diagnostics and the bounded local coding agent."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from rich.console import Console

from proofcoder.agent_runtime import (
    AgentRunLimits,
    build_agent_loop,
    create_agent_runtime_resources,
    emit_setup_termination,
)
from proofcoder.config import ProofCoderConfig
from proofcoder.context import DEFAULT_CONTEXT_BUDGET_BYTES
from proofcoder.errors import ConfigurationError, ProofCoderError
from proofcoder.eval_core import AgentRunner
from proofcoder.eval_runner import (
    DEFAULT_FIXTURES_ROOT,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_REPEAT,
    MAX_REPEAT,
    MIN_REPEAT,
    EvaluationInfrastructureError,
    EvaluationModelInfo,
    EvaluationProgress,
    EvaluationProgressKind,
    create_evaluation_agent_runner,
    run_evaluation,
)
from proofcoder.events import TerminalSink
from proofcoder.llm.base import LLMClient
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.protocol import CompletionStatus, ModelResponse, TerminationReason
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS
from proofcoder.safety.secrets import redact_text, sensitive_environment_values
from proofcoder.trace import (
    TracePathError,
    final_trace_report,
    list_traces,
    read_trace,
)

_MINIMUM_PYTHON = (3, 11)


class _ConnectivityClient(Protocol):
    def check_connection(self) -> ModelResponse: ...


_ConnectivityClientFactory = Callable[[ProofCoderConfig], _ConnectivityClient]
_DEFAULT_CONNECTIVITY_CLIENT_FACTORY = cast(_ConnectivityClientFactory, DeepSeekClient)
_RunClientFactory = Callable[[ProofCoderConfig], LLMClient]
_DEFAULT_RUN_CLIENT_FACTORY = cast(_RunClientFactory, DeepSeekClient)
_MAX_AGENT_STEPS = 64
_MAX_AGENT_SECONDS = 3600.0
_MIN_CONTEXT_BUDGET_BYTES = 4096
_MAX_CONTEXT_BUDGET_BYTES = 2 * 1024 * 1024
_MAX_CONSECUTIVE_FAILURES = 32


@dataclass(frozen=True, slots=True)
class _CheckResult:
    name: str
    ok: bool
    detail: str


def build_parser() -> argparse.ArgumentParser:
    """Build the standard-library argument parser."""

    parser = argparse.ArgumentParser(prog="proofcoder", description="ProofCoder command line")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="check local setup and API connectivity")
    doctor.add_argument(
        "--offline",
        action="store_true",
        help="run local checks without reading an API key or accessing the network",
    )
    run = commands.add_parser("run", help="run the local coding agent loop")
    run.add_argument("--workspace", required=True, help="existing workspace directory")
    run.add_argument(
        "--max-steps",
        type=_bounded_max_steps,
        default=8,
        help=f"maximum successful assistant responses, 1-{_MAX_AGENT_STEPS} (default: 8)",
    )
    run.add_argument(
        "--max-seconds",
        type=_bounded_max_seconds,
        default=600.0,
        help=f"maximum wall-clock seconds, 1-{_MAX_AGENT_SECONDS:g} (default: 600)",
    )
    run.add_argument(
        "--context-budget-bytes",
        type=_bounded_context_budget,
        default=DEFAULT_CONTEXT_BUDGET_BYTES,
        help=(
            f"request context budget, {_MIN_CONTEXT_BUDGET_BYTES}-"
            f"{_MAX_CONTEXT_BUDGET_BYTES} bytes (default: {DEFAULT_CONTEXT_BUDGET_BYTES})"
        ),
    )
    run.add_argument(
        "--max-consecutive-failures",
        type=_bounded_consecutive_failures,
        default=5,
        help=f"consecutive failed batches, 1-{_MAX_CONSECUTIVE_FAILURES} (default: 5)",
    )
    run.add_argument(
        "--max-api-attempts",
        type=_bounded_api_attempts,
        default=DEFAULT_MAX_API_ATTEMPTS,
        help=f"attempts per model response, 1-{DEFAULT_MAX_API_ATTEMPTS} (default: 3)",
    )
    run.add_argument("task", help="task for the local coding agent loop")
    evaluate = commands.add_parser(
        "eval",
        help="run repeated local fixtures using real model calls",
        description=(
            "Run ProofCoder's local evaluation fixtures. This command makes real model calls; "
            "tests must inject a fake client or runner."
        ),
    )
    evaluate.add_argument(
        "--repeat",
        type=_bounded_repeat,
        default=DEFAULT_REPEAT,
        help=f"attempts per fixture, {MIN_REPEAT}-{MAX_REPEAT} (default: {DEFAULT_REPEAT})",
    )
    evaluate.add_argument(
        "--fixture",
        action="append",
        default=[],
        help="fixture ID to run; repeat the option to select multiple (default: all)",
    )
    evaluate.add_argument(
        "--fixtures-root",
        default=str(DEFAULT_FIXTURES_ROOT),
        help=f"fixture directory relative to the project root (default: {DEFAULT_FIXTURES_ROOT})",
    )
    evaluate.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"result directory relative to the project root (default: {DEFAULT_OUTPUT_ROOT})",
    )
    evaluate.add_argument(
        "--max-steps",
        type=_bounded_max_steps,
        default=8,
        help=f"maximum assistant responses per attempt, 1-{_MAX_AGENT_STEPS} (default: 8)",
    )
    evaluate.add_argument(
        "--max-seconds",
        type=_bounded_max_seconds,
        default=600.0,
        help=f"maximum wall-clock seconds per attempt, 1-{_MAX_AGENT_SECONDS:g}",
    )
    evaluate.add_argument(
        "--context-budget-bytes",
        type=_bounded_context_budget,
        default=DEFAULT_CONTEXT_BUDGET_BYTES,
        help=(
            f"request context budget per attempt, {_MIN_CONTEXT_BUDGET_BYTES}-"
            f"{_MAX_CONTEXT_BUDGET_BYTES} bytes"
        ),
    )
    evaluate.add_argument(
        "--max-consecutive-failures",
        type=_bounded_consecutive_failures,
        default=5,
        help=f"consecutive failed batches per attempt, 1-{_MAX_CONSECUTIVE_FAILURES}",
    )
    evaluate.add_argument(
        "--max-api-attempts",
        type=_bounded_api_attempts,
        default=DEFAULT_MAX_API_ATTEMPTS,
        help=f"API attempts per model response, 1-{DEFAULT_MAX_API_ATTEMPTS}",
    )
    trace = commands.add_parser("trace", help="inspect safe workspace JSONL traces")
    trace_commands = trace.add_subparsers(dest="trace_command", required=True)
    trace_list = trace_commands.add_parser("list", help="list workspace run traces")
    trace_list.add_argument("--workspace", required=True, help="existing workspace directory")
    trace_show = trace_commands.add_parser("show", help="show one safe run trace")
    trace_show.add_argument("--workspace", required=True, help="existing workspace directory")
    trace_show.add_argument("run_id", help="32-character lowercase hexadecimal run ID")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    console: Console | None = None,
    client_factory: _ConnectivityClientFactory = _DEFAULT_CONNECTIVITY_CLIENT_FACTORY,
    run_client_factory: _RunClientFactory = _DEFAULT_RUN_CLIENT_FACTORY,
    eval_client_factory: _RunClientFactory = _DEFAULT_RUN_CLIENT_FACTORY,
    eval_agent_runner: AgentRunner | None = None,
) -> int:
    """Run the ProofCoder CLI and return a process exit code."""

    args = build_parser().parse_args(argv)
    output = console or Console()
    base_cwd = Path.cwd() if cwd is None else cwd
    if args.command == "doctor":
        return _run_doctor(
            offline=bool(args.offline),
            environ=environ,
            cwd=base_cwd,
            console=output,
            client_factory=client_factory,
        )
    if args.command == "run":
        try:
            return _run_agent(
                task=str(args.task),
                workspace_argument=str(args.workspace),
                max_steps=int(args.max_steps),
                max_seconds=float(args.max_seconds),
                context_budget_bytes=int(args.context_budget_bytes),
                max_consecutive_failures=int(args.max_consecutive_failures),
                max_api_attempts=int(args.max_api_attempts),
                environ=environ,
                cwd=base_cwd,
                console=output,
                client_factory=run_client_factory,
            )
        except KeyboardInterrupt:
            _print(output, "DONE: termination=interrupted completion=none")
            return 130
    if args.command == "eval":
        limits = AgentRunLimits(
            max_steps=int(args.max_steps),
            max_seconds=float(args.max_seconds),
            context_budget_bytes=int(args.context_budget_bytes),
            max_consecutive_failures=int(args.max_consecutive_failures),
            max_api_attempts=int(args.max_api_attempts),
        )
        sensitive_values = sensitive_environment_values(environ)
        try:
            config = ProofCoderConfig.from_env(environ=environ)
            runner = (
                eval_agent_runner
                if eval_agent_runner is not None
                else create_evaluation_agent_runner(
                    config=config,
                    limits=limits,
                    environ=environ,
                    client_factory=eval_client_factory,
                )
            )
            session = run_evaluation(
                project_root=base_cwd,
                fixtures_root=Path(str(args.fixtures_root)),
                output_root=Path(str(args.output_root)),
                fixture_ids=tuple(str(item) for item in args.fixture),
                repeat=int(args.repeat),
                agent_runner=runner,
                model=EvaluationModelInfo(
                    name=redact_text(config.model, sensitive_values=sensitive_values),
                    base_url=redact_text(config.base_url, sensitive_values=sensitive_values),
                    reasoning_effort=config.reasoning_effort,
                ),
                limits=limits,
                environ=environ,
                on_progress=lambda progress: _render_eval_progress(output, progress),
            )
        except ConfigurationError:
            _print(output, "FAIL eval: CONFIGURATION_ERROR (check model environment)")
            return 2
        except EvaluationInfrastructureError as error:
            _print(output, f"FAIL eval: {error.code} ({error})")
            return 2
        except KeyboardInterrupt:
            _print(output, "SUMMARY status=interrupted attempts=0 successes=0")
            return 130
        return session.exit_code
    if args.command == "trace":
        return _run_trace(
            trace_command=str(args.trace_command),
            workspace_argument=str(args.workspace),
            run_id=None if not hasattr(args, "run_id") else str(args.run_id),
            cwd=base_cwd,
            console=output,
        )
    return 2


def _bounded_max_steps(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max steps must be an integer") from None
    if not 1 <= parsed <= _MAX_AGENT_STEPS:
        raise argparse.ArgumentTypeError(f"max steps must be between 1 and {_MAX_AGENT_STEPS}")
    return parsed


def _bounded_repeat(value: str) -> int:
    return _bounded_integer(
        value,
        label="repeat",
        minimum=MIN_REPEAT,
        maximum=MAX_REPEAT,
    )


def _bounded_max_seconds(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max seconds must be a number") from None
    if not 1 <= parsed <= _MAX_AGENT_SECONDS:
        raise argparse.ArgumentTypeError(
            f"max seconds must be between 1 and {_MAX_AGENT_SECONDS:g}"
        )
    return parsed


def _bounded_context_budget(value: str) -> int:
    return _bounded_integer(
        value,
        label="context budget bytes",
        minimum=_MIN_CONTEXT_BUDGET_BYTES,
        maximum=_MAX_CONTEXT_BUDGET_BYTES,
    )


def _bounded_consecutive_failures(value: str) -> int:
    return _bounded_integer(
        value,
        label="max consecutive failures",
        minimum=1,
        maximum=_MAX_CONSECUTIVE_FAILURES,
    )


def _bounded_api_attempts(value: str) -> int:
    return _bounded_integer(
        value,
        label="max API attempts",
        minimum=1,
        maximum=DEFAULT_MAX_API_ATTEMPTS,
    )


def _bounded_integer(value: str, *, label: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from None
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _run_doctor(
    *,
    offline: bool,
    environ: Mapping[str, str] | None,
    cwd: Path,
    console: Console,
    client_factory: _ConnectivityClientFactory,
) -> int:
    try:
        config = ProofCoderConfig.from_env(offline=offline, environ=environ)
    except ConfigurationError as error:
        _print(console, f"FAIL Configuration: {error}")
        return 1

    secret = config.api_key
    checks = _local_checks(cwd)
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        _safe_print(console, f"{status} {check.name}: {check.detail}", secret)

    _safe_print(console, f"Configuration base URL: {config.base_url}", secret)
    _safe_print(console, f"Configuration model: {config.model}", secret)
    _safe_print(
        console,
        f"Configuration reasoning effort: {config.reasoning_effort}",
        secret,
    )

    if not all(check.ok for check in checks):
        return 1
    if offline:
        _print(console, "PASS API connectivity: skipped in offline mode")
        return 0

    try:
        client_factory(config).check_connection()
    except Exception:
        _print(
            console,
            "FAIL API connectivity: DeepSeek request failed; check configuration and network.",
        )
        return 1

    _print(console, "PASS API connectivity: DeepSeek connection succeeded")
    return 0


def _run_agent(
    *,
    task: str,
    workspace_argument: str,
    max_steps: int,
    max_seconds: float,
    context_budget_bytes: int,
    max_consecutive_failures: int,
    max_api_attempts: int,
    environ: Mapping[str, str] | None,
    cwd: Path,
    console: Console,
    client_factory: _RunClientFactory,
) -> int:
    workspace_input = Path(workspace_argument)
    workspace = (
        workspace_input.resolve(strict=False)
        if workspace_input.is_absolute()
        else (cwd / workspace_input).resolve(strict=False)
    )
    if not workspace.exists() or not workspace.is_dir():
        _print(
            console,
            "DONE: termination=invalid_workspace completion=none\n"
            "  workspace must be an existing directory",
        )
        return 2

    secret: str | None = None
    sensitive_values = sensitive_environment_values(environ)
    try:
        resources = create_agent_runtime_resources(
            workspace,
            environ=environ,
            sensitive_values=sensitive_values,
        )
    except TracePathError as error:
        _print(
            console,
            f"DONE: termination=trace_error completion=none\n  error_code={error.code}",
        )
        return 1
    terminal = TerminalSink(lambda line: _safe_print(console, line, secret))
    try:
        config = ProofCoderConfig.from_env(environ=environ)
    except ConfigurationError:
        emit_setup_termination(
            task=task,
            resources=resources,
            termination_reason=TerminationReason.CONFIGURATION_ERROR,
            additional_sinks=(terminal,),
            sensitive_values=sensitive_values,
        )
        resources.close()
        return 1
    secret = config.api_key

    try:
        client = client_factory(config)
    except KeyboardInterrupt:
        emit_setup_termination(
            task=task,
            resources=resources,
            termination_reason=TerminationReason.INTERRUPTED,
            additional_sinks=(terminal,),
            sensitive_values=sensitive_values,
        )
        resources.close()
        return 130
    except SystemExit:
        resources.close()
        raise
    except ProofCoderError:
        emit_setup_termination(
            task=task,
            resources=resources,
            termination_reason=TerminationReason.API_ERROR,
            additional_sinks=(terminal,),
            sensitive_values=sensitive_values,
        )
        resources.close()
        return 1

    try:
        result = build_agent_loop(
            client=client,
            resources=resources,
            limits=AgentRunLimits(
                max_steps=max_steps,
                max_seconds=max_seconds,
                context_budget_bytes=context_budget_bytes,
                max_consecutive_failures=max_consecutive_failures,
                max_api_attempts=max_api_attempts,
            ),
            additional_sinks=(terminal,),
            sensitive_values=sensitive_values,
        ).run(task)
    finally:
        resources.close()

    if result.final_report is not None:
        _safe_print(console, "REPORT:", secret)
        for report_line in result.final_report.splitlines():
            _safe_print(console, f"  {report_line}", secret)
    return _run_exit_code(result.termination_reason, result.completion_status)


def _run_trace(
    *,
    trace_command: str,
    workspace_argument: str,
    run_id: str | None,
    cwd: Path,
    console: Console,
) -> int:
    workspace_input = Path(workspace_argument)
    workspace = (
        workspace_input.resolve(strict=False)
        if workspace_input.is_absolute()
        else (cwd / workspace_input).resolve(strict=False)
    )
    if not workspace.exists() or not workspace.is_dir():
        _print(
            console,
            "FAIL trace: workspace must be an existing directory",
        )
        return 2
    try:
        if trace_command == "list":
            summaries = list_traces(workspace)
            _print(console, "run_id started_at status events trace_complete")
            for summary in summaries:
                _print(
                    console,
                    f"{summary.run_id} {summary.started_at} {summary.status} "
                    f"{summary.event_count} {str(summary.trace_complete).lower()}",
                )
            return 0
        if trace_command == "show" and run_id is not None:
            trace = read_trace(workspace, run_id)
            terminal = TerminalSink(lambda line: _print(console, line))
            for event in trace.events:
                terminal.emit(event)
            for issue in trace.issues:
                location = "" if issue.line_number is None else f" line={issue.line_number}"
                _print(
                    console,
                    f"WARN: {issue.code}{location} ({issue.message})",
                )
            _print(console, f"REPORT: {final_trace_report(trace)}")
            return 0 if trace.trace_complete else 1
    except TracePathError as error:
        _print(console, f"FAIL trace: {error.code} ({error})")
        return 1
    return 2


def _render_eval_progress(console: Console, progress: EvaluationProgress) -> None:
    """Render compact evaluation-only progress without model or command bodies."""

    if progress.kind is EvaluationProgressKind.STARTED:
        _print(console, f"EVAL {progress.eval_id}")
        return
    if progress.kind is EvaluationProgressKind.ATTEMPT_STARTED:
        _print(
            console,
            f"RUN {progress.fixture_id} attempt={progress.attempt_index}/{progress.repeat}",
        )
        return
    if progress.kind is EvaluationProgressKind.ATTEMPT_FINISHED:
        result = progress.result
        if result is None:
            return
        completion = "none" if result.completion_status is None else result.completion_status.value
        validation_exit = (
            None if result.final_validation is None else result.final_validation.exit_code
        )
        reasons = ",".join(reason.value for reason in result.failure_reasons) or "none"
        elapsed_seconds = f"{result.elapsed_seconds:.3f}".rstrip("0").rstrip(".")
        _print(
            console,
            f"RESULT success={str(result.success).lower()} completion={completion} "
            f"validation_exit={validation_exit} reasons={reasons} "
            f"elapsed_seconds={elapsed_seconds}",
        )
        return
    if progress.kind is EvaluationProgressKind.FINISHED:
        aggregate = progress.aggregate
        if aggregate is None:
            return
        status = "failed" if progress.status is None else progress.status.value
        failure = "" if progress.failure_code is None else f" failure_code={progress.failure_code}"
        _print(
            console,
            f"SUMMARY status={status} attempts={aggregate.overall.attempts} "
            f"successes={aggregate.overall.successes}{failure}",
        )
        _print(console, f"ARTIFACT {progress.evaluation_directory}")


def _run_exit_code(
    termination_reason: TerminationReason,
    completion_status: CompletionStatus | None,
) -> int:
    if termination_reason is TerminationReason.INTERRUPTED:
        return 130
    if termination_reason is not TerminationReason.FINISH_TASK:
        return 1
    return {
        CompletionStatus.COMPLETED_VERIFIED: 0,
        CompletionStatus.COMPLETED_NO_CHANGES: 0,
        CompletionStatus.COMPLETED_UNVERIFIED: 3,
        CompletionStatus.BLOCKED: 4,
        None: 1,
    }[completion_status]


def _local_checks(cwd: Path) -> tuple[_CheckResult, ...]:
    python_ok = sys.version_info >= _MINIMUM_PYTHON
    python_version = ".".join(str(part) for part in sys.version_info[:3])

    try:
        importlib.import_module("proofcoder")
    except Exception:
        package_ok = False
    else:
        package_ok = True

    workspace_ok = cwd.is_dir() and os.access(cwd, os.R_OK | os.W_OK)
    return (
        _CheckResult("Python", python_ok, f"{python_version} (requires 3.11+)"),
        _CheckResult("ProofCoder import", package_ok, "available" if package_ok else "failed"),
        _CheckResult(
            "Working directory",
            workspace_ok,
            f"{cwd} ({'readable and writable' if workspace_ok else 'not readable and writable'})",
        ),
    )


def _safe_print(console: Console, message: str, secret: str | None) -> None:
    if secret:
        message = message.replace(secret, "[redacted]")
    _print(console, message)


def _print(console: Console, message: str) -> None:
    """Write one line without Rich's width-based wrapping.

    Rich inserts real newlines when a line exceeds the console width, which splits
    run IDs, trace paths, and argv mid-token and makes them impossible to copy.
    Soft wrapping leaves that to the terminal instead.
    """

    console.print(message, markup=False, soft_wrap=True)
