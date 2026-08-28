"""Offline tests for the default-deny command policy and environment filtering."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import proofcoder.safety.commands as command_policy
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import PreparedToolCall, RiskLevel, ToolResult
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.registry import ToolRegistry

FAKE_SECRET = "obviously-fake-command-secret-for-tests"


@pytest.fixture(autouse=True)
def _stable_executable_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        command_policy.shutil,
        "which",
        lambda executable, *, path: str(Path(sys.executable).resolve()),
    )


def _environment(**extra: str) -> dict[str, str]:
    environment = {
        "PATH": str(Path(sys.executable).resolve().parent),
        "PATHEXT": ".EXE;.CMD",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", os.fspath(Path.cwd())),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
    }
    environment.update(extra)
    return environment


def _prepare(
    workspace: Path,
    arguments: object,
    *,
    environ: dict[str, str] | None = None,
) -> PreparedToolCall | ToolResult:
    registry = ToolRegistry()
    registry.register(create_run_command_tool(workspace, environ=environ or _environment()))
    call = ToolCall(
        id="command-1",
        function=FunctionCall(name="run_command", arguments=json.dumps(arguments)),
    )
    return registry.prepare(call)


def _assert_error(result: PreparedToolCall | ToolResult, code: str) -> ToolResult:
    assert isinstance(result, ToolResult)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == code
    return result


def test_tool_protocol_is_argv_only_and_describes_residual_risk(tmp_path: Path) -> None:
    tool = create_run_command_tool(tmp_path, environ=_environment())

    properties = tool.parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"argv", "cwd", "timeout_seconds"}
    assert tool.parameters["required"] == ["argv"]
    assert tool.parameters["additionalProperties"] is False
    assert tool.risk_level is RiskLevel.EXECUTE
    assert "not an operating-system sandbox" in tool.description
    assert "best effort" in tool.description


@pytest.mark.parametrize(
    "arguments",
    [
        {"argv": "pytest"},
        {"argv": []},
        {"argv": ["pytest", 1]},
        {"argv": ["pytest", ""]},
        {"argv": ["pytest", "bad\0value"]},
        {"argv": ["pytest", *("-q" for _ in range(64))]},
        {"argv": ["pytest", "x" * 4097]},
        {"argv": ["pytest", *("x" * 4000 for _ in range(9))]},
        {"argv": ["pytest"], "timeout_seconds": True},
        {"argv": ["pytest"], "timeout_seconds": 0},
        {"argv": ["pytest"], "timeout_seconds": 301},
    ],
)
def test_invalid_command_shapes_are_rejected(tmp_path: Path, arguments: object) -> None:
    _assert_error(_prepare(tmp_path, arguments), "INVALID_ARGUMENTS")


@pytest.mark.parametrize("unknown", ["command", "shell", "env", "stdin", "input", "approval"])
def test_unknown_and_unsafe_protocol_fields_are_rejected(tmp_path: Path, unknown: str) -> None:
    _assert_error(
        _prepare(tmp_path, {"argv": ["pytest"], unknown: "unsafe"}),
        "INVALID_ARGUMENTS",
    )


def test_defaults_and_normal_workspace_cwd_are_normalized(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()

    prepared = _prepare(tmp_path, {"argv": ["python3", "-m", "unittest"], "cwd": "nested"})

    assert isinstance(prepared, PreparedToolCall)
    command = command_policy.prepare_command(
        tmp_path,
        prepared.arguments,
        environ=_environment(),
    )
    assert command.execution_argv[0] == sys.executable
    assert command.display_argv == ("python", "-m", "unittest")
    assert command.relative_cwd == "nested"
    assert command.timeout_seconds == 60


@pytest.mark.parametrize("cwd", ["missing", "cwd.txt", "../outside"])
def test_invalid_cwd_is_rejected(tmp_path: Path, cwd: str) -> None:
    (tmp_path / "cwd.txt").write_text("not a directory", encoding="utf-8")

    result = _prepare(tmp_path, {"argv": ["pytest"], "cwd": cwd})

    assert isinstance(result, ToolResult)
    assert result.error is not None
    assert result.error.code in {"PATH_NOT_FOUND", "NOT_A_DIRECTORY", "PATH_OUTSIDE_WORKSPACE"}


def test_absolute_cwd_is_rejected_even_when_it_names_workspace(tmp_path: Path) -> None:
    _assert_error(
        _prepare(tmp_path, {"argv": ["pytest"], "cwd": str(tmp_path.resolve())}),
        "PATH_OUTSIDE_WORKSPACE",
    )


def test_external_cwd_symlink_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "external-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    _assert_error(
        _prepare(workspace, {"argv": ["pytest"], "cwd": "external-link"}),
        "PATH_OUTSIDE_WORKSPACE",
    )


def test_external_absolute_executable_is_rejected(tmp_path: Path) -> None:
    _assert_error(
        _prepare(tmp_path, {"argv": [sys.executable, "-m", "pytest"]}),
        "PATH_OUTSIDE_WORKSPACE",
    )


def test_explicit_workspace_executable_still_uses_allowlist(tmp_path: Path) -> None:
    executable = tmp_path / "pytest"
    executable.write_text("not executed", encoding="utf-8")

    command = command_policy.prepare_command(
        tmp_path,
        {"argv": ["./pytest", "-q"]},
        environ=_environment(),
    )

    assert command.execution_argv[0] == str(executable.resolve())
    assert command.display_argv == ("pytest", "-q")


def test_bare_executable_lookup_discards_empty_and_relative_path_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    malicious = tmp_path / ("pytest.exe" if os.name == "nt" else "pytest")
    malicious.write_text("not executed", encoding="utf-8")
    observed: dict[str, str] = {}

    def lookup(executable: str, *, path: str) -> str:
        observed["executable"] = executable
        observed["path"] = path
        return sys.executable

    monkeypatch.setattr(command_policy.shutil, "which", lookup)
    absolute_entry = str(Path(sys.executable).resolve().parent)
    raw_path = os.pathsep.join(["", ".", "relative-bin", absolute_entry])

    command_policy.prepare_command(
        tmp_path,
        {"argv": ["pytest"]},
        environ=_environment(PATH=raw_path),
    )

    assert observed == {"executable": "pytest", "path": absolute_entry}
    assert str(tmp_path) not in observed["path"]


@pytest.mark.parametrize(
    "argv,kind",
    [
        (["python", "-m", "unittest", "-v"], "test"),
        (["python.exe", "-m", "pytest", "-q"], "test"),
        (["py", "-m", "compileall", "."], "build"),
        (["python3", "-m", "ruff", "check", "."], "static_check"),
        (["ruff", "check", "."], "static_check"),
        (["ruff", "format", "--check", "."], "static_check"),
        (["pytest", "tests"], "test"),
        (["git", "status"], "git_read"),
        (["git", "diff"], "git_read"),
        (["git", "log", "-1"], "git_read"),
        (["git", "show", "HEAD"], "git_read"),
        (["git", "rev-parse", "--show-toplevel"], "git_read"),
        (["git", "ls-files"], "git_read"),
    ],
)
def test_allowlisted_commands_prepare_with_expected_kind(
    tmp_path: Path,
    argv: list[str],
    kind: str,
) -> None:
    command = command_policy.prepare_command(
        tmp_path,
        {"argv": argv},
        environ=_environment(),
    )

    assert command.command_kind == kind


def test_workspace_python_script_is_resolved_but_display_path_stays_relative(
    tmp_path: Path,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "check.py"
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")

    command = command_policy.prepare_command(
        tmp_path,
        {"argv": ["python", "scripts/check.py", "argument"]},
        environ=_environment(),
    )

    assert command.execution_argv == (sys.executable, str(script.resolve()), "argument")
    assert command.display_argv == ("python", "scripts/check.py", "argument")
    assert command.command_kind == "script"


@pytest.mark.parametrize(
    "argv",
    [
        ["cmd"],
        ["cmd.exe"],
        ["powershell"],
        ["pwsh"],
        ["bash"],
        ["sh"],
        ["zsh"],
        ["fish"],
        ["wsl"],
        ["tool.bat"],
        ["tool.cmd"],
        ["tool.ps1"],
        ["tool.sh"],
        ["rm"],
        ["del"],
        ["chmod"],
        ["sudo"],
        ["runas"],
        ["curl"],
        ["wget"],
        ["ssh"],
        ["pip"],
        ["npm"],
        ["unknown-program"],
        ["python", "-c", "print('unsafe')"],
        ["python", "-"],
        ["python", "-m", "pip"],
        ["python", "-m", "venv", "venv"],
        ["python", "-m", "ensurepip"],
        ["ruff", "check", "--fix", "."],
        ["ruff", "check", "--fix-only", "."],
        ["ruff", "check", "--unsafe-fixes", "."],
        ["ruff", "format", "."],
        ["ruff", "clean"],
    ],
)
def test_dangerous_or_unknown_commands_are_blocked(tmp_path: Path, argv: list[str]) -> None:
    _assert_error(_prepare(tmp_path, {"argv": argv}), "COMMAND_BLOCKED")


@pytest.mark.parametrize(
    "subcommand",
    [
        "add",
        "commit",
        "push",
        "pull",
        "fetch",
        "checkout",
        "switch",
        "restore",
        "reset",
        "clean",
        "rebase",
        "merge",
        "cherry-pick",
        "revert",
        "tag",
        "stash",
        "config",
        "worktree",
        "gc",
    ],
)
def test_git_write_and_network_subcommands_are_blocked(tmp_path: Path, subcommand: str) -> None:
    _assert_error(_prepare(tmp_path, {"argv": ["git", subcommand]}), "COMMAND_BLOCKED")


@pytest.mark.parametrize(
    "option",
    [
        "-C",
        "-C..",
        "--git-dir",
        "--git-dir=../outside",
        "--work-tree",
        "--work-tree=../outside",
        "--no-index",
        "--output",
        "--output=report.txt",
        "--ext-diff",
        "--textconv",
    ],
)
def test_git_scope_output_and_external_program_options_are_blocked(
    tmp_path: Path,
    option: str,
) -> None:
    _assert_error(_prepare(tmp_path, {"argv": ["git", "diff", option]}), "COMMAND_BLOCKED")


@pytest.mark.parametrize(
    "argv",
    [
        ["pytest", "../outside"],
        ["pytest", "--rootdir=../outside"],
        ["pytest", "--junitxml", "../outside.xml"],
        ["pytest", "--cov-report=xml:../coverage.xml"],
        ["ruff", "check", "../outside"],
        ["ruff", "check", "--output-file=../outside.txt", "."],
        ["git", "diff", "../outside"],
    ],
)
def test_workspace_external_path_arguments_are_blocked(tmp_path: Path, argv: list[str]) -> None:
    _assert_error(_prepare(tmp_path, {"argv": argv}), "COMMAND_BLOCKED")


def test_pytest_basetemp_cannot_target_workspace_root(tmp_path: Path) -> None:
    _assert_error(
        _prepare(tmp_path, {"argv": ["pytest", "--basetemp", "."]}),
        "COMMAND_BLOCKED",
    )


def test_command_environment_is_minimal_noninteractive_and_secret_free(tmp_path: Path) -> None:
    environment = _environment(
        DEEPSEEK_API_KEY=FAKE_SECRET,
        SERVICE_TOKEN="fake-token",
        DATABASE_PASSWORD="fake-password",
        CLIENT_CREDENTIAL="fake-credential",
        ORDINARY_PARENT_VALUE="must-not-pass",
        LC_ALL="C",
    )

    command = command_policy.prepare_command(
        tmp_path,
        {"argv": ["python", "-m", "unittest"]},
        environ=environment,
    )

    assert command.environment["PATH"] == str(Path(sys.executable).resolve().parent)
    assert command.environment["SYSTEMROOT"] == environment["SYSTEMROOT"]
    assert command.environment["WINDIR"] == environment["WINDIR"]
    assert command.environment["PATHEXT"] == environment["PATHEXT"]
    assert command.environment["LC_ALL"] == "C"
    assert command.environment["GIT_TERMINAL_PROMPT"] == "0"
    assert command.environment["GIT_PAGER"] == "cat"
    assert command.environment["NO_COLOR"] == "1"
    assert command.environment["PYTHONUTF8"] == "1"
    assert command.environment["PYTHONIOENCODING"] == "utf-8"
    assert "DEEPSEEK_API_KEY" not in command.environment
    assert "SERVICE_TOKEN" not in command.environment
    assert "DATABASE_PASSWORD" not in command.environment
    assert "CLIENT_CREDENTIAL" not in command.environment
    assert "ORDINARY_PARENT_VALUE" not in command.environment
    assert FAKE_SECRET in command.sensitive_values


def test_known_fake_secret_in_argv_is_blocked_before_execution(tmp_path: Path) -> None:
    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["pytest", FAKE_SECRET]},
            environ=_environment(DEEPSEEK_API_KEY=FAKE_SECRET),
        ),
        "COMMAND_BLOCKED",
    )
