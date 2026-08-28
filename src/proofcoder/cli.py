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

from proofcoder.agent import AgentLoop
from proofcoder.config import ProofCoderConfig
from proofcoder.context import DEFAULT_CONTEXT_BUDGET_BYTES
from proofcoder.errors import ConfigurationError, ProofCoderError
from proofcoder.events import CompositeSink, EventEmitter, EventType, TerminalSink, new_run_id
from proofcoder.llm.base import LLMClient
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.prompt import STAGE_B_SYSTEM_PROMPT
from proofcoder.protocol import CompletionStatus, ModelResponse, TerminationReason
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS
from proofcoder.safety.secrets import sensitive_environment_values
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import create_create_file_tool, create_replace_in_file_tool
from proofcoder.tools.files import create_list_files_tool, create_read_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry
from proofcoder.tools.search import create_search_text_tool
from proofcoder.trace import (
    TracePathError,
    TraceRecorder,
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
            output.print("DONE: termination=interrupted", markup=False)
            return 130
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
        console.print(f"FAIL Configuration: {error}", markup=False)
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
        console.print("PASS API connectivity: skipped in offline mode", markup=False)
        return 0

    try:
        client_factory(config).check_connection()
    except Exception:
        console.print(
            "FAIL API connectivity: DeepSeek request failed; check configuration and network.",
            markup=False,
        )
        return 1

    console.print("PASS API connectivity: DeepSeek connection succeeded", markup=False)
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
        console.print(
            "DONE: termination=invalid_workspace (workspace must be an existing directory)",
            markup=False,
        )
        return 2

    secret: str | None = None
    sensitive_values = sensitive_environment_values(environ)
    run_id = new_run_id()
    try:
        recorder = TraceRecorder(
            workspace,
            run_id,
            sensitive_values=sensitive_values,
        )
    except TracePathError as error:
        console.print(
            f"DONE: termination=trace_error error_code={error.code}",
            markup=False,
        )
        return 1
    terminal = TerminalSink(lambda line: _safe_print(console, line, secret))
    try:
        config = ProofCoderConfig.from_env(environ=environ)
    except ConfigurationError:
        _emit_setup_termination(
            task=task,
            run_id=run_id,
            trace_path=recorder.trace_path,
            termination_reason=TerminationReason.CONFIGURATION_ERROR,
            sink=CompositeSink(terminal, recorder),
            sensitive_values=sensitive_values,
        )
        recorder.close()
        return 1
    secret = config.api_key

    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace))
    registry.register(create_search_text_tool(workspace))
    registry.register(create_read_file_tool(workspace))
    registry.register(create_create_file_tool(workspace))
    registry.register(create_replace_in_file_tool(workspace))
    registry.register(create_run_command_tool(workspace, environ=environ))
    registry.register(create_finish_task_tool(workspace))
    try:
        client = client_factory(config)
    except KeyboardInterrupt:
        _emit_setup_termination(
            task=task,
            run_id=run_id,
            trace_path=recorder.trace_path,
            termination_reason=TerminationReason.INTERRUPTED,
            sink=CompositeSink(terminal, recorder),
            sensitive_values=sensitive_values,
        )
        recorder.close()
        return 130
    except SystemExit:
        recorder.close()
        raise
    except ProofCoderError:
        _emit_setup_termination(
            task=task,
            run_id=run_id,
            trace_path=recorder.trace_path,
            termination_reason=TerminationReason.API_ERROR,
            sink=CompositeSink(terminal, recorder),
            sensitive_values=sensitive_values,
        )
        recorder.close()
        return 1

    try:
        result = AgentLoop(
            client=client,
            registry=registry,
            workspace=workspace,
            system_prompt=STAGE_B_SYSTEM_PROMPT,
            max_steps=max_steps,
            max_seconds=max_seconds,
            context_budget_bytes=context_budget_bytes,
            max_consecutive_failures=max_consecutive_failures,
            max_api_attempts=max_api_attempts,
            event_sink=CompositeSink(terminal, recorder),
            run_id_factory=lambda: run_id,
            sensitive_values=sensitive_values,
            trace_path=recorder.trace_path,
        ).run(task)
    finally:
        recorder.close()

    if result.final_report is not None:
        _safe_print(console, f"REPORT: {result.final_report}", secret)
    return _run_exit_code(result.termination_reason, result.completion_status)


def _emit_setup_termination(
    *,
    task: str,
    run_id: str,
    trace_path: str,
    termination_reason: TerminationReason,
    sink: CompositeSink,
    sensitive_values: tuple[str, ...],
) -> None:
    """Persist a minimal complete trajectory when setup stops before AgentLoop."""

    emitter = EventEmitter(
        run_id=run_id,
        sink=sink,
        sensitive_values=sensitive_values,
    )
    emitter.emit(EventType.TASK, step=0, payload={"task": task})
    emitter.emit(
        EventType.TERMINATION,
        step=0,
        payload={
            "api_attempts": 0,
            "api_retries": 0,
            "changed_files": [],
            "completion_status": "none",
            "context_compactions": 0,
            "elapsed_seconds": 0.0,
            "event_count": emitter.event_count + 1,
            "input_tokens": 0,
            "model_calls": 0,
            "output_tokens": 0,
            "termination_reason": termination_reason.value,
            "tool_calls": 0,
            "tool_errors": 0,
            "trace_complete": emitter.trace_complete,
            "trace_path": trace_path,
            "verification": None,
            "warning_count": 0,
        },
    )


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
        console.print(
            "FAIL trace: workspace must be an existing directory",
            markup=False,
        )
        return 2
    try:
        if trace_command == "list":
            summaries = list_traces(workspace)
            console.print("run_id started_at status events trace_complete", markup=False)
            for summary in summaries:
                console.print(
                    f"{summary.run_id} {summary.started_at} {summary.status} "
                    f"{summary.event_count} {str(summary.trace_complete).lower()}",
                    markup=False,
                )
            return 0
        if trace_command == "show" and run_id is not None:
            trace = read_trace(workspace, run_id)
            terminal = TerminalSink(lambda line: console.print(line, markup=False))
            for event in trace.events:
                terminal.emit(event)
            for issue in trace.issues:
                location = "" if issue.line_number is None else f" line={issue.line_number}"
                console.print(
                    f"WARN: {issue.code}{location} ({issue.message})",
                    markup=False,
                )
            console.print(f"REPORT: {final_trace_report(trace)}", markup=False)
            return 0 if trace.trace_complete else 1
    except TracePathError as error:
        console.print(f"FAIL trace: {error.code} ({error})", markup=False)
        return 1
    return 2


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
    console.print(message, markup=False)
