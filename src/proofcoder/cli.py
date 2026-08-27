"""Command-line interface for diagnostics and the Stage B read-only agent."""

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
from proofcoder.errors import ConfigurationError, ProofCoderError
from proofcoder.llm.base import LLMClient
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.prompt import STAGE_B_SYSTEM_PROMPT
from proofcoder.protocol import AssistantMessage, ModelResponse, TerminationReason, ToolMessage
from proofcoder.tools.files import create_list_files_tool
from proofcoder.tools.registry import ToolRegistry

_MINIMUM_PYTHON = (3, 11)


class _ConnectivityClient(Protocol):
    def check_connection(self) -> ModelResponse: ...


_ConnectivityClientFactory = Callable[[ProofCoderConfig], _ConnectivityClient]
_DEFAULT_CONNECTIVITY_CLIENT_FACTORY = cast(_ConnectivityClientFactory, DeepSeekClient)
_RunClientFactory = Callable[[ProofCoderConfig], LLMClient]
_DEFAULT_RUN_CLIENT_FACTORY = cast(_RunClientFactory, DeepSeekClient)
_MAX_AGENT_STEPS = 64


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
    run = commands.add_parser("run", help="run the Stage B read-only agent loop")
    run.add_argument("--workspace", required=True, help="existing workspace directory")
    run.add_argument(
        "--max-steps",
        type=_bounded_max_steps,
        default=8,
        help=f"maximum model calls, from 1 to {_MAX_AGENT_STEPS} (default: 8)",
    )
    run.add_argument("task", help="task for the read-only discovery loop")
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
        return _run_agent(
            task=str(args.task),
            workspace_argument=str(args.workspace),
            max_steps=int(args.max_steps),
            environ=environ,
            cwd=base_cwd,
            console=output,
            client_factory=run_client_factory,
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
    environ: Mapping[str, str] | None,
    cwd: Path,
    console: Console,
    client_factory: _RunClientFactory,
) -> int:
    try:
        config = ProofCoderConfig.from_env(environ=environ)
    except ConfigurationError as error:
        console.print(f"DONE: termination=configuration_error ({error})", markup=False)
        return 1

    workspace_input = Path(workspace_argument)
    workspace = (
        workspace_input.resolve(strict=False)
        if workspace_input.is_absolute()
        else (cwd / workspace_input).resolve(strict=False)
    )
    secret = config.api_key
    if not workspace.exists() or not workspace.is_dir():
        _safe_print(
            console,
            "DONE: termination=invalid_workspace (workspace must be an existing directory)",
            secret,
        )
        return 2

    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace))
    try:
        client = client_factory(config)
    except ProofCoderError:
        console.print("DONE: termination=api_error", markup=False)
        return 1

    _safe_print(console, f"TASK: {task}", secret)
    result = AgentLoop(
        client=client,
        registry=registry,
        workspace=workspace,
        system_prompt=STAGE_B_SYSTEM_PROMPT,
        max_steps=max_steps,
    ).run(task)

    for message in result.history.messages:
        if isinstance(message, AssistantMessage):
            visible = message.content if message.content else "<no visible text>"
            _safe_print(console, f"MODEL: {visible}", secret)
            for call in message.tool_calls:
                _safe_print(
                    console,
                    f"TOOL: {call.function.name} (id={call.id})",
                    secret,
                )
        elif isinstance(message, ToolMessage):
            _safe_print(
                console,
                f"RESULT: id={message.tool_call_id} {message.content}",
                secret,
            )

    _safe_print(
        console,
        (
            f"DONE: termination={result.termination_reason.value} "
            f"model_calls={result.model_call_count} tool_calls={result.tool_call_count} "
            f"tool_errors={result.tool_error_count}"
        ),
        secret,
    )
    return {
        TerminationReason.MODEL_STOPPED: 0,
        TerminationReason.API_ERROR: 1,
        TerminationReason.MAX_STEPS: 3,
    }[result.termination_reason]


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
