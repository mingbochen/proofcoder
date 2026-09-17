"""Command-line interface for diagnostics and the bounded local coding agent."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from rich.console import Console

from proofcoder.agent_runtime import (
    AgentRunLimits,
    build_agent_loop,
    create_agent_runtime_resources,
    emit_setup_termination,
    run_exit_code,
)
from proofcoder.approval import (
    ApprovalGate,
    ApprovalMode,
    ApprovalOutcome,
    ApprovalRequest,
    ApprovalResponder,
)
from proofcoder.checkpoint import (
    ChangeSource,
    CheckpointError,
    RollbackAction,
    RollbackPlan,
    delete_checkpoint,
    list_checkpoints,
)
from proofcoder.config import ProofCoderConfig, ProviderName
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
from proofcoder.llm.factory import create_client, create_connectivity_client
from proofcoder.protocol import ModelResponse, TerminationReason
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS
from proofcoder.rollback import build_rollback_plan, perform_rollback, rollback_exit_code
from proofcoder.safety.commands import load_project_command_policy
from proofcoder.safety.policy import (
    POLICY_FILENAME,
    CommandPolicy,
    CommandPolicyFileError,
    unloaded_policy_warning,
    workspace_policy_path,
)
from proofcoder.safety.secrets import redact_text, sensitive_environment_values
from proofcoder.session import (
    SessionCarry,
    SessionError,
    append_run_record,
    build_session_carry,
    create_session,
    delete_session,
    end_session,
    list_sessions,
    load_session,
    run_record_from_result,
)
from proofcoder.trace import (
    TracePathError,
    final_trace_report,
    list_traces,
    read_trace,
)
from proofcoder.web.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ServerAddressError,
    WebServer,
    create_server,
)

_MINIMUM_PYTHON = (3, 11)


class _ConnectivityClient(Protocol):
    def check_connection(self) -> ModelResponse: ...


_ConnectivityClientFactory = Callable[[ProofCoderConfig], _ConnectivityClient]
_DEFAULT_CONNECTIVITY_CLIENT_FACTORY = cast(_ConnectivityClientFactory, create_connectivity_client)
_RunClientFactory = Callable[[ProofCoderConfig], LLMClient]
_DEFAULT_RUN_CLIENT_FACTORY = cast(_RunClientFactory, create_client)
_MAX_AGENT_STEPS = 64
_MAX_AGENT_SECONDS = 3600.0
_MIN_CONTEXT_BUDGET_BYTES = 4096
_MAX_CONTEXT_BUDGET_BYTES = 2 * 1024 * 1024
_MAX_CONSECUTIVE_FAILURES = 32
_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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
    run.add_argument(
        "--no-checkpoint",
        action="store_true",
        help=(
            "start without a rollback checkpoint; the run's writes, and any made by "
            "workspace scripts it starts, cannot be undone afterwards"
        ),
    )
    run.add_argument(
        "--approval",
        choices=[ApprovalMode.NEVER.value, ApprovalMode.ON_RISK.value],
        default=ApprovalMode.NEVER.value,
        help=(
            "how commands that need confirmation are handled: 'never' refuses them "
            f"(default), 'on-risk' asks at the terminal (default: {ApprovalMode.NEVER.value})"
        ),
    )
    run.add_argument(
        "--command-policy",
        default=None,
        help=(
            "path to a project command policy file to authorize for this run; "
            "a policy is never loaded unless it is named here"
        ),
    )
    run.add_argument(
        "--session",
        default=None,
        help=(
            "continue the named session, or start one with 'new'; without this the run "
            "reads and writes no session data"
        ),
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
    serve = commands.add_parser(
        "serve",
        help="serve the local browser interface for the agent loop",
        description=(
            "Start a loopback HTTP server that runs the same bounded agent loop as "
            "`proofcoder run` and renders its sanitized events in a browser page."
        ),
    )
    serve.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"interface to bind (default: {DEFAULT_HOST}; loopback only unless overridden)",
    )
    serve.add_argument(
        "--port",
        type=_bounded_port,
        default=DEFAULT_PORT,
        help=f"TCP port, 0 for an ephemeral port (default: {DEFAULT_PORT})",
    )
    serve.add_argument(
        "--workspace",
        default=None,
        help="directory offered as the initial workspace (default: current directory)",
    )
    serve.add_argument(
        "--no-browse",
        action="store_true",
        help="disable the directory picker so only typed workspace paths are accepted",
    )
    serve.add_argument(
        "--open",
        action="store_true",
        help="open the interface in the default browser after binding",
    )
    rollback = commands.add_parser(
        "rollback",
        help="inspect run checkpoints and undo one run's workspace changes",
        description=(
            "Work with the checkpoints recorded before each run. 'apply' restores the "
            "workspace to one run's baseline after showing everything it would change."
        ),
    )
    rollback_commands = rollback.add_subparsers(dest="rollback_command", required=True)
    rollback_list = rollback_commands.add_parser("list", help="list stored run checkpoints")
    rollback_list.add_argument("--workspace", required=True, help="existing workspace directory")
    rollback_show = rollback_commands.add_parser(
        "show",
        help="show what rolling back one run would change, without changing anything",
    )
    rollback_show.add_argument("--workspace", required=True, help="existing workspace directory")
    rollback_show.add_argument("run_id", help="32-character lowercase hexadecimal run ID")
    rollback_apply = rollback_commands.add_parser(
        "apply",
        help="restore the workspace to one run's baseline",
    )
    rollback_apply.add_argument("--workspace", required=True, help="existing workspace directory")
    rollback_apply.add_argument(
        "--yes",
        action="store_true",
        help="apply without the confirmation prompt, for unattended use",
    )
    rollback_apply.add_argument("run_id", help="32-character lowercase hexadecimal run ID")
    rollback_delete = rollback_commands.add_parser(
        "delete",
        help="remove one stored checkpoint and free its space",
    )
    rollback_delete.add_argument("--workspace", required=True, help="existing workspace directory")
    rollback_delete.add_argument("run_id", help="32-character lowercase hexadecimal run ID")
    session = commands.add_parser(
        "session",
        help="inspect and manage cross-run sessions in one workspace",
    )
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_list = session_commands.add_parser("list", help="list stored sessions")
    session_list.add_argument("--workspace", required=True, help="existing workspace directory")
    session_show = session_commands.add_parser("show", help="show one session and its runs")
    session_show.add_argument("--workspace", required=True, help="existing workspace directory")
    session_show.add_argument("session_id", help="32-character lowercase hexadecimal session ID")
    session_end = session_commands.add_parser(
        "end",
        help="end one session; its records stay readable but it accepts no further runs",
    )
    session_end.add_argument("--workspace", required=True, help="existing workspace directory")
    session_end.add_argument("session_id", help="32-character lowercase hexadecimal session ID")
    session_delete = session_commands.add_parser("delete", help="delete one stored session")
    session_delete.add_argument("--workspace", required=True, help="existing workspace directory")
    session_delete.add_argument("session_id", help="32-character lowercase hexadecimal session ID")
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
    serve_forever: Callable[[WebServer], None] | None = None,
    confirm_rollback: Callable[[], bool] | None = None,
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
                checkpoint_enabled=not bool(args.no_checkpoint),
                approval_mode=ApprovalMode(str(args.approval)),
                policy_argument=(None if args.command_policy is None else str(args.command_policy)),
                session_argument=(None if args.session is None else str(args.session)),
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
    if args.command == "serve":
        try:
            return _run_server(
                host=str(args.host),
                port=int(args.port),
                workspace_argument=None if args.workspace is None else str(args.workspace),
                allow_browse=not bool(args.no_browse),
                open_browser=bool(args.open),
                environ=environ,
                cwd=base_cwd,
                console=output,
                serve_forever=serve_forever,
            )
        except KeyboardInterrupt:
            _print(output, "SERVE stopped")
            return 130
    if args.command == "rollback":
        try:
            return _run_rollback(
                rollback_command=str(args.rollback_command),
                workspace_argument=str(args.workspace),
                run_id=None if not hasattr(args, "run_id") else str(args.run_id),
                assume_yes=bool(getattr(args, "yes", False)),
                cwd=base_cwd,
                console=output,
                confirm=confirm_rollback,
            )
        except KeyboardInterrupt:
            _print(output, "ROLLBACK interrupted: nothing further was changed")
            return 130
    if args.command == "session":
        return _run_session_command(args, cwd=base_cwd, console=output)
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


def _bounded_port(value: str) -> int:
    return _bounded_integer(value, label="port", minimum=0, maximum=65535)


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

    _safe_print(console, f"Configuration provider: {config.provider.value}", secret)
    _safe_print(console, f"Configuration base URL: {config.base_url}", secret)
    _safe_print(console, f"Configuration model: {config.model}", secret)
    if config.provider is ProviderName.DEEPSEEK:
        # Reasoning effort is a DeepSeek setting. Printing it for a provider that does
        # not read it would describe configuration that is not in force.
        _safe_print(
            console,
            f"Configuration reasoning effort: {config.reasoning_effort}",
            secret,
        )
    if not config.requires_api_key:
        _print(console, "PASS Credentials: this provider needs none")

    if not all(check.ok for check in checks):
        return 1
    if offline:
        _print(console, "PASS API connectivity: skipped in offline mode")
        return 0

    label = _provider_label(config.provider)
    try:
        client_factory(config).check_connection()
    except Exception:
        _print(
            console,
            f"FAIL API connectivity: {label} request failed; check configuration and network.",
        )
        return 1

    _print(console, f"PASS API connectivity: {label} connection succeeded")
    return 0


def _provider_label(provider: ProviderName) -> str:
    return "local model" if provider is ProviderName.OLLAMA else "DeepSeek"


def _terminal_responder(console: Console) -> ApprovalResponder:
    """Ask at the terminal, and refuse rather than assume when nobody is there.

    A console read cannot be given a portable wall-clock timeout, so this responder
    does not time out; the unattended case is handled by refusing outright when
    standard input is not a terminal, exactly as the rollback confirmation does.
    """

    def respond(request: ApprovalRequest) -> ApprovalOutcome:
        if not sys.stdin.isatty():
            _print(
                console,
                "APPROVAL refused: standard input is not a terminal; "
                "run without --approval on-risk for unattended use",
            )
            return ApprovalOutcome.DENIED
        argv = json.dumps(list(request.display_argv), ensure_ascii=False)
        _print(console, "APPROVAL needed before this command runs:")
        _print(console, f"  argv={argv}")
        _print(console, f"  cwd={request.relative_cwd} timeout_seconds={request.timeout_seconds}")
        _print(
            console,
            f"  kind={request.command_kind} decided_by={request.decision_source}",
        )
        answer = input("run this command? [y/N] ")
        return (
            ApprovalOutcome.APPROVED
            if answer.strip().casefold() in {"y", "yes"}
            else ApprovalOutcome.DENIED
        )

    return respond


def _resolve_workspace(workspace_argument: str, cwd: Path) -> Path | None:
    """Resolve one workspace argument, or return None when it is not a directory."""

    workspace_input = Path(workspace_argument)
    workspace = (
        workspace_input.resolve(strict=False)
        if workspace_input.is_absolute()
        else (cwd / workspace_input).resolve(strict=False)
    )
    if not workspace.exists() or not workspace.is_dir():
        return None
    return workspace


def _run_session_command(args: argparse.Namespace, *, cwd: Path, console: Console) -> int:
    """Run one `proofcoder session` subcommand."""

    workspace = _resolve_workspace(str(args.workspace), cwd)
    if workspace is None:
        _print(console, "FAIL session: INVALID_WORKSPACE (must be an existing directory)")
        return 2
    command = str(args.session_command)
    try:
        if command == "list":
            summaries = list_sessions(workspace)
            if not summaries:
                _print(console, "SESSIONS: none")
                return 0
            for summary in summaries:
                state = "ended" if summary.ended_at is not None else "open"
                _print(
                    console,
                    f"SESSION {summary.session_id} state={state} runs={summary.run_count} "
                    f"bytes={summary.byte_count} created={summary.created_at}",
                )
            return 0
        if command == "show":
            session = load_session(workspace, str(args.session_id))
            state = "ended" if session.ended_at is not None else "open"
            _print(
                console,
                f"SESSION {session.session_id} state={state} runs={len(session.runs)} "
                f"created={session.created_at}",
            )
            for index, record in enumerate(session.runs, start=1):
                status = "none" if record.completion_status is None else record.completion_status
                _print(
                    console,
                    f"  RUN {index} {record.run_id} {record.termination_reason}/{status} "
                    f"changed={len(record.changed_files)} recorded={record.recorded_at}",
                )
                if record.verification is not None:
                    # Reported expired here too: a listing is an audit surface, and a bare
                    # exit code would read like evidence the next run inherits.
                    _print(
                        console,
                        f"    verification(expired) {' '.join(record.verification.argv)} "
                        f"exit={record.verification.exit_code}",
                    )
            return 0
        if command == "end":
            session = end_session(workspace, str(args.session_id))
            _print(console, f"SESSION {session.session_id} ended={session.ended_at}")
            return 0
        delete_session(workspace, str(args.session_id))
        _print(console, f"SESSION {args.session_id} deleted")
        return 0
    except SessionError as error:
        _print(console, f"FAIL session: {error.code} ({error})")
        return 2


def _open_session_carry(
    workspace: Path,
    session_argument: str,
    *,
    context_budget_bytes: int,
) -> tuple[str, SessionCarry]:
    """Resolve the named session and assemble what this run carries from it."""

    if session_argument == "new":
        session = create_session(workspace)
    else:
        session = load_session(workspace, session_argument)
        if session.ended:
            raise SessionError(
                "SESSION_ENDED", "this session has ended and accepts no further runs"
            )
    return session.session_id, build_session_carry(
        session, context_budget_bytes=context_budget_bytes
    )


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
    checkpoint_enabled: bool = True,
    approval_mode: ApprovalMode = ApprovalMode.NEVER,
    policy_argument: str | None = None,
    approval_responder: ApprovalResponder | None = None,
    session_argument: str | None = None,
) -> int:
    resolved = _resolve_workspace(workspace_argument, cwd)
    if resolved is None:
        _print(
            console,
            "DONE: termination=invalid_workspace completion=none\n"
            "  workspace must be an existing directory",
        )
        return 2
    workspace = resolved

    secret: str | None = None
    sensitive_values = sensitive_environment_values(environ)
    policy: CommandPolicy | None = None
    if policy_argument is not None:
        policy_input = Path(policy_argument)
        policy_path = policy_input if policy_input.is_absolute() else (cwd / policy_input)
        try:
            policy = load_project_command_policy(policy_path, workspace=workspace)
        except CommandPolicyFileError as error:
            _print(
                console,
                "DONE: termination=configuration_error completion=none\n"
                f"  error_code={error.code}\n  {error}",
            )
            return 1
    session_id: str | None = None
    carry: SessionCarry | None = None
    if session_argument is not None:
        try:
            session_id, carry = _open_session_carry(
                workspace,
                session_argument,
                context_budget_bytes=context_budget_bytes,
            )
        except SessionError as error:
            _print(
                console,
                "DONE: termination=configuration_error completion=none\n"
                f"  error_code={error.code}\n  {error}",
            )
            return 1
    gate = ApprovalGate(
        mode=approval_mode,
        responder=_terminal_responder(console)
        if approval_responder is None and approval_mode is ApprovalMode.ON_RISK
        else approval_responder,
    )
    try:
        resources = create_agent_runtime_resources(
            workspace,
            environ=environ,
            sensitive_values=sensitive_values,
            checkpoint_enabled=checkpoint_enabled,
            policy=policy,
            approval=gate,
            carry=carry,
        )
    except TracePathError as error:
        _print(
            console,
            f"DONE: termination=trace_error completion=none\n  error_code={error.code}",
        )
        return 1
    terminal = TerminalSink(lambda line: _safe_print(console, line, secret))
    if policy is None and workspace_policy_path(workspace).is_file():
        # Discoverable without being self-granting: the file is named, not applied.
        _print(console, f"WARN: {unloaded_policy_warning(POLICY_FILENAME)['message']}")
    if resources.checkpoint_error is not None:
        _print(console, f"WARN: {resources.checkpoint_error.code}")
        emit_setup_termination(
            task=task,
            resources=resources,
            termination_reason=TerminationReason.CHECKPOINT_ERROR,
            additional_sinks=(terminal,),
            sensitive_values=sensitive_values,
        )
        resources.close()
        return 1
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

    if session_id is not None:
        # Recorded after the run, from the finished result, so what the session carries
        # forward is what the program observed rather than anything the model asserted.
        try:
            append_run_record(
                workspace,
                session_id,
                run_record_from_result(result, task=task, sensitive_values=sensitive_values),
            )
        except SessionError as error:
            _safe_print(console, f"WARN: SESSION_NOT_RECORDED {error.code}", secret)
        else:
            _safe_print(console, f"SESSION: {session_id} recorded this run", secret)

    if result.final_report is not None:
        _safe_print(console, "REPORT:", secret)
        for report_line in result.final_report.splitlines():
            _safe_print(console, f"  {report_line}", secret)
    return run_exit_code(result.termination_reason, result.completion_status)


def _run_server(
    *,
    host: str,
    port: int,
    workspace_argument: str | None,
    allow_browse: bool,
    open_browser: bool,
    environ: Mapping[str, str] | None,
    cwd: Path,
    console: Console,
    server_factory: Callable[..., WebServer] = create_server,
    serve_forever: Callable[[WebServer], None] | None = None,
) -> int:
    """Bind the local interface, report its address, and serve until interrupted."""

    workspace = cwd
    if workspace_argument is not None:
        candidate = Path(workspace_argument)
        workspace = (
            candidate.resolve(strict=False)
            if candidate.is_absolute()
            else (cwd / candidate).resolve(strict=False)
        )
        if not workspace.is_dir():
            _print(console, "FAIL serve: workspace must be an existing directory")
            return 2

    try:
        server = server_factory(
            host=host,
            port=port,
            environ=environ,
            cwd=cwd,
            workspace=workspace,
            allow_browse=allow_browse,
        )
    except ServerAddressError as error:
        _print(console, f"FAIL serve: {error.code} ({error})")
        return 2

    if host not in _LOOPBACK_BIND_HOSTS:
        _print(
            console,
            "WARN serve: a non-loopback bind exposes local file and command authority "
            "to this network; stop the server unless the interface is trusted.",
        )
    _print(console, f"SERVE {server.url}")
    _print(console, f"  workspace={workspace}")
    _print(console, "  the page's session token is never printed; open the URL above")
    if open_browser:
        _open_browser(server.url)
    runner = serve_forever if serve_forever is not None else _serve_forever
    runner(server)
    return 0


def _serve_forever(server: WebServer) -> None:
    """Serve until interrupted, then stop every run the browser started."""

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


def _open_browser(url: str) -> None:
    """Open the default browser without failing the server when none exists."""

    import webbrowser

    with suppress(Exception):
        webbrowser.open(url)


_MAX_LISTED_PATHS = 50
_ROLLBACK_ACTION_LABELS = {
    RollbackAction.RESTORE: "restore",
    RollbackAction.RECREATE: "recreate",
    RollbackAction.DELETE: "delete",
    RollbackAction.CREATE_DIRECTORY: "create dir",
    RollbackAction.REMOVE_DIRECTORY: "remove dir",
}


def _run_rollback(
    *,
    rollback_command: str,
    workspace_argument: str,
    run_id: str | None,
    assume_yes: bool,
    cwd: Path,
    console: Console,
    confirm: Callable[[], bool] | None = None,
) -> int:
    """Inspect checkpoints and undo one run, never writing before it is confirmed."""

    workspace_input = Path(workspace_argument)
    workspace = (
        workspace_input.resolve(strict=False)
        if workspace_input.is_absolute()
        else (cwd / workspace_input).resolve(strict=False)
    )
    if not workspace.exists() or not workspace.is_dir():
        _print(console, "FAIL rollback: workspace must be an existing directory")
        return 2

    try:
        if rollback_command == "list":
            return _print_checkpoint_list(workspace, console)
        if run_id is None:
            _print(console, "FAIL rollback: a run ID is required")
            return 2
        if rollback_command == "delete":
            freed = delete_checkpoint(workspace, run_id)
            _print(console, f"CHECKPOINT deleted: run_id={run_id} freed_bytes={freed}")
            return 0
        plan = build_rollback_plan(workspace, run_id)
        if rollback_command == "show":
            _print_rollback_plan(plan, console)
            return 0
        if rollback_command == "apply":
            return _apply_rollback(
                workspace=workspace,
                plan=plan,
                assume_yes=assume_yes,
                console=console,
                confirm=confirm,
            )
    except CheckpointError as error:
        _print(console, f"FAIL rollback: {error.code} ({error})")
        return 1
    return 2


def _apply_rollback(
    *,
    workspace: Path,
    plan: RollbackPlan,
    assume_yes: bool,
    console: Console,
    confirm: Callable[[], bool] | None,
) -> int:
    """Confirm one plan, apply it, and report every path it could not restore."""

    _print_rollback_plan(plan, console)
    if plan.empty:
        # Reporting the gaps still matters, but there is nothing to confirm.
        return 0
    if not assume_yes:
        decision = confirm() if confirm is not None else _confirm_rollback(console)
        if not decision:
            _print(console, "ROLLBACK declined: the workspace was not changed")
            return 3

    recorded = perform_rollback(
        workspace,
        plan,
        additional_sinks=(TerminalSink(lambda line: _print(console, line)),),
    )
    result = recorded.result
    _print_labelled_paths(
        console, "failed", [f"{failure.path} ({failure.code})" for failure in result.failures]
    )
    if not recorded.trace_complete:
        _print(console, "WARN: the rollback trace is incomplete")
    return rollback_exit_code(recorded)


def _confirm_rollback(console: Console) -> bool:
    """Ask once on a terminal, and refuse rather than assume when there is none."""

    if not sys.stdin.isatty():
        _print(
            console,
            "ROLLBACK needs confirmation: standard input is not a terminal; "
            "re-run with --yes to apply without the prompt",
        )
        return False
    try:
        answer = input("apply this rollback? [y/N] ")
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _print_checkpoint_list(workspace: Path, console: Console) -> int:
    summaries = list_checkpoints(workspace)
    _print(console, "run_id created_at entries stored_bytes readable")
    for summary in summaries:
        _print(
            console,
            f"{summary.run_id} {summary.created_at or 'unknown'} {summary.entry_count} "
            f"{summary.stored_bytes} {str(summary.readable).lower()}",
        )
    return 0


def _print_rollback_plan(plan: RollbackPlan, console: Console) -> None:
    """Show every planned action and every reported gap before anything is written."""

    counts = " ".join(
        f"{label}={len(plan.paths_for(action))}"
        for action, label in _ROLLBACK_ACTION_LABELS.items()
        if plan.paths_for(action)
    )
    header = f"PLAN: target={plan.run_id}"
    _print(console, f"{header} {counts}" if counts else f"{header} no changes to undo")
    for action, label in _ROLLBACK_ACTION_LABELS.items():
        _print_labelled_paths(
            console,
            label,
            [
                item.path if item.source is ChangeSource.OTHER else f"{item.path} (tool)"
                for item in plan.items
                if item.action is action
            ],
        )
    _print_labelled_paths(
        console,
        "not covered",
        [f"{skip.path} ({skip.reason})" for skip in plan.skipped],
    )


def _print_labelled_paths(console: Console, label: str, entries: Sequence[str]) -> None:
    """Print a bounded listing so one huge plan cannot flood the terminal."""

    if not entries:
        return
    for entry in entries[:_MAX_LISTED_PATHS]:
        _print(console, f"  {label}: {entry}")
    remaining = len(entries) - _MAX_LISTED_PATHS
    if remaining > 0:
        _print(console, f"  {label}: ... and {remaining} more")


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
