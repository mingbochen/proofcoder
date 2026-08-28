"""Bounded local command execution with policy, redaction, and audit output."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from proofcoder.safety.commands import (
    MAX_COMMAND_ARGUMENT_CHARS,
    MAX_COMMAND_ARGUMENTS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    MIN_COMMAND_TIMEOUT_SECONDS,
    CommandPolicyError,
    PreparedCommand,
    prepare_command,
)
from proofcoder.safety.paths import WorkspacePathError
from proofcoder.safety.writes import (
    commit_new_file,
    discard_temporary_file,
    stage_temporary_file,
)
from proofcoder.tools.base import RiskLevel, ToolDefinition, ToolResult

MAX_RETURN_STREAM_BYTES = 32 * 1024
MAX_AUDIT_STREAM_BYTES = 10 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_GRACE_SECONDS = 0.75
_READER_JOIN_SECONDS = 2.0
_AUDIT_DIRECTORY = Path(".proofcoder/runtime/commands")
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


@dataclass(slots=True)
class _BoundedCapture:
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    @property
    def hard_truncated(self) -> bool:
        return self.total_bytes > MAX_AUDIT_STREAM_BYTES

    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        head_limit = MAX_AUDIT_STREAM_BYTES // 2
        tail_limit = MAX_AUDIT_STREAM_BYTES - head_limit
        if len(self.head) < head_limit:
            accepted = min(len(chunk), head_limit - len(self.head))
            self.head.extend(chunk[:accepted])
            chunk = chunk[accepted:]
        if chunk:
            self.tail.extend(chunk)
            if len(self.tail) > tail_limit:
                del self.tail[: len(self.tail) - tail_limit]

    def retained_bytes(self) -> bytes:
        return bytes(self.head + self.tail)


def create_run_command_tool(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> ToolDefinition:
    """Create a policy-bound local command tool for one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def preflight(arguments: Mapping[str, object]) -> ToolResult | None:
        prepared = _prepare_or_result(workspace_root, arguments, environ=environ)
        return prepared if isinstance(prepared, ToolResult) else None

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        prepared = _prepare_or_result(workspace_root, arguments, environ=environ)
        if isinstance(prepared, ToolResult):
            return prepared
        return _run_prepared_command(workspace_root, prepared)

    return ToolDefinition(
        name="run_command",
        description=(
            "Run an allowlisted local test, static check, build check, workspace Python script, "
            "or read-only Git command using argv and shell=false. Unknown, shell, network, "
            "installer, mutation, and unsafe path forms are blocked before execution. Output is "
            "bounded, redacted, and audited under .proofcoder. Repository scripts still execute "
            "repository code, and timeout process-tree cleanup is best effort on every platform: "
            "this policy reduces risk but is not an operating-system sandbox."
        ),
        parameters={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "description": "Non-empty command argv; shell strings are not accepted.",
                    "minItems": 1,
                    "maxItems": MAX_COMMAND_ARGUMENTS,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_COMMAND_ARGUMENT_CHARS,
                    },
                },
                "cwd": {
                    "type": "string",
                    "description": "Workspace-relative existing directory.",
                    "minLength": 1,
                    "default": ".",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Wall-clock timeout in seconds.",
                    "minimum": MIN_COMMAND_TIMEOUT_SECONDS,
                    "maximum": MAX_COMMAND_TIMEOUT_SECONDS,
                    "default": 60,
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        execute=execute,
        preflight=preflight,
        risk_level=RiskLevel.EXECUTE,
    )


def _prepare_or_result(
    workspace: Path,
    arguments: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None,
) -> PreparedCommand | ToolResult:
    try:
        return prepare_command(workspace, arguments, environ=environ)
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)
    except CommandPolicyError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)


def _run_prepared_command(workspace: Path, command: PreparedCommand) -> ToolResult:
    started = time.monotonic()
    process_kwargs: dict[str, object] = {}
    if os.name == "nt":
        process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        process_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            list(command.execution_argv),
            cwd=command.cwd,
            env=command.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **process_kwargs,
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "COMMAND_NOT_FOUND",
            "allowed executable disappeared before it could be started; retry after checking PATH",
            retryable=True,
            duration_ms=_duration_ms(started),
        )
    except OSError:
        return ToolResult.failure(
            "COMMAND_SPAWN_ERROR",
            "command could not be started; inspect the executable and workspace permissions",
            retryable=True,
            duration_ms=_duration_ms(started),
        )

    stdout_capture = _BoundedCapture()
    stderr_capture = _BoundedCapture()
    assert process.stdout is not None
    assert process.stderr is not None
    readers = (
        threading.Thread(
            target=_pump_stream,
            args=(process.stdout, stdout_capture),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_stream,
            args=(process.stderr, stderr_capture),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        try:
            process.wait(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process, command.environment)
    finally:
        if process.poll() is None:
            _terminate_process_tree(process, command.environment)
        _finish_readers(process, readers)

    duration_ms = _duration_ms(started)
    stdout_audit = _sanitize_capture(stdout_capture, command.sensitive_values)
    stderr_audit = _sanitize_capture(stderr_capture, command.sensitive_values)
    stdout, stdout_return_truncated = _bound_text(stdout_audit)
    stderr, stderr_return_truncated = _bound_text(stderr_audit)
    stdout_truncated = stdout_capture.hard_truncated or stdout_return_truncated
    stderr_truncated = stderr_capture.hard_truncated or stderr_return_truncated
    warnings: list[str] = []
    audit_truncated = stdout_capture.hard_truncated or stderr_capture.hard_truncated
    if audit_truncated:
        warnings.append(
            "AUDIT_OUTPUT_TRUNCATED: a stream exceeded the 10 MiB local audit hard limit"
        )

    audit_path: str | None = None
    try:
        audit_path = _write_audit_file(
            workspace,
            command,
            exit_code=process.returncode,
            timed_out=timed_out,
            duration_ms=duration_ms,
            stdout=stdout_audit,
            stderr=stderr_audit,
            stdout_bytes=stdout_capture.total_bytes,
            stderr_bytes=stderr_capture.total_bytes,
            audit_truncated=audit_truncated,
        )
    except OSError:
        warnings.append(
            "AUDIT_WRITE_FAILED: redacted command output could not be saved to the workspace"
        )

    data: dict[str, object] = {
        "argv": list(command.display_argv),
        "cwd": command.relative_cwd,
        "command_kind": command.command_kind,
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_bytes": stdout_capture.total_bytes,
        "stderr_bytes": stderr_capture.total_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "timed_out": timed_out,
        "audit_path": audit_path,
        "audit_truncated": audit_truncated,
    }
    truncated = stdout_truncated or stderr_truncated
    if timed_out:
        return ToolResult.failure(
            "COMMAND_TIMEOUT",
            "command exceeded timeout_seconds and was terminated; narrow the command or retry",
            retryable=True,
            data=data,
            duration_ms=duration_ms,
            truncated=truncated,
            warnings=tuple(warnings),
        )
    return ToolResult.success(
        data,
        duration_ms=duration_ms,
        truncated=truncated,
        warnings=tuple(warnings),
    )


def _pump_stream(stream: BinaryIO, capture: _BoundedCapture) -> None:
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            capture.append(chunk)
    except (OSError, ValueError):
        return


def _finish_readers(
    process: subprocess.Popen[bytes],
    readers: tuple[threading.Thread, threading.Thread],
) -> None:
    for reader in readers:
        reader.join(timeout=_READER_JOIN_SECONDS)
    if any(reader.is_alive() for reader in readers):
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        for reader in readers:
            reader.join(timeout=_READER_JOIN_SECONDS)


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    environment: Mapping[str, str],
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        with suppress(OSError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
        taskkill = _windows_taskkill_path(environment)
        if taskkill is not None:
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=dict(environment),
                    timeout=5,
                    check=False,
                )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            with suppress(OSError):
                process.terminate()
        _wait_for_exit(process, _PROCESS_GRACE_SECONDS)
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)

    if not _wait_for_exit(process, _PROCESS_GRACE_SECONDS):
        with suppress(OSError):
            process.kill()
        _wait_for_exit(process, _PROCESS_GRACE_SECONDS)


def _wait_for_exit(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _windows_taskkill_path(environment: Mapping[str, str]) -> str | None:
    system_root = environment.get("SYSTEMROOT") or environment.get("WINDIR")
    if not system_root:
        return None
    candidate = Path(system_root) / "System32" / "taskkill.exe"
    return str(candidate) if candidate.is_file() else None


def _sanitize_capture(capture: _BoundedCapture, sensitive_values: tuple[str, ...]) -> str:
    if not capture.hard_truncated:
        return _sanitize_text(
            capture.retained_bytes().decode("utf-8", errors="replace"), sensitive_values
        )

    head = _sanitize_text(bytes(capture.head).decode("utf-8", errors="replace"), sensitive_values)
    tail = _sanitize_text(bytes(capture.tail).decode("utf-8", errors="replace"), sensitive_values)
    omitted = capture.total_bytes - len(capture.head) - len(capture.tail)
    return f"{head}\n... audit stream truncated: {omitted} bytes omitted ...\n{tail}"


def _sanitize_text(text: str, sensitive_values: tuple[str, ...]) -> str:
    sanitized = _ANSI_OSC.sub("", _ANSI_CSI.sub("", text))
    sanitized = sanitized.replace("\r\n", "\n").replace("\r", "\n")
    sanitized = "".join(
        character
        for character in sanitized
        if character in {"\n", "\t"} or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    for sensitive_value in sensitive_values:
        sanitized = sanitized.replace(sensitive_value, "[redacted]")
    return sanitized


def _bound_text(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_RETURN_STREAM_BYTES:
        return text, False

    marker = b""
    head = b""
    tail = b""
    for _ in range(4):
        available = max(0, MAX_RETURN_STREAM_BYTES - len(marker))
        head_budget = available // 2
        tail_budget = available - head_budget
        head = encoded[:head_budget]
        tail = encoded[-tail_budget:] if tail_budget else b""
        omitted = len(encoded) - len(head) - len(tail)
        marker = f"\n... output truncated: {omitted} bytes omitted ...\n".encode()
    available = max(0, MAX_RETURN_STREAM_BYTES - len(marker))
    head_budget = available // 2
    tail_budget = available - head_budget
    head_text = encoded[:head_budget].decode("utf-8", errors="ignore")
    tail_text = encoded[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    return f"{head_text}{marker.decode()}{tail_text}", True


def _write_audit_file(
    workspace: Path,
    command: PreparedCommand,
    *,
    exit_code: int | None,
    timed_out: bool,
    duration_ms: int,
    stdout: str,
    stderr: str,
    stdout_bytes: int,
    stderr_bytes: int,
    audit_truncated: bool,
) -> str:
    audit_directory = workspace / _AUDIT_DIRECTORY
    audit_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        audit_directory.chmod(0o700)
    filename = f"command-{time.time_ns()}-{uuid.uuid4().hex}.json"
    target = audit_directory / filename
    payload = json.dumps(
        {
            "argv": list(command.display_argv),
            "cwd": command.relative_cwd,
            "command_kind": command.command_kind,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_ms": duration_ms,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "audit_truncated": audit_truncated,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(target, payload, mode=0o600)
        commit_new_file(temporary, target)
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)
    return target.relative_to(workspace).as_posix()


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
