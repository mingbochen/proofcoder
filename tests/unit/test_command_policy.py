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
def _stable_executable_lookup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    toolchain = tmp_path.parent / f"{tmp_path.name}-toolchain"
    toolchain.mkdir()
    compiler_header = b"MZ" if os.name == "nt" else b"\x7fELF"
    compilers: dict[str, Path] = {}
    for name in ("g++", "g++.exe"):
        compiler = toolchain / name
        compiler.write_bytes(compiler_header)
        compiler.chmod(0o755)
        compilers[name] = compiler

    def lookup(executable: str, *, path: str) -> str:
        compiler_name = executable.casefold()
        if os.name == "nt" and compiler_name == "g++":
            compiler_name = "g++.exe"
        compiler = compilers.get(compiler_name)
        return str(compiler if compiler is not None else Path(sys.executable).resolve())

    monkeypatch.setattr(command_policy.shutil, "which", lookup)


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


@pytest.mark.parametrize(
    "argv",
    [
        ["g++", "-std=c++17", "-O2", "-o", "main.exe", "main.cpp"],
        ["g++.exe", "main.cpp", "-o", "main.exe", "-std=c++17"],
        ["g++", "-o", "main.exe", "main.cpp", "-std=c++17", "-O2"],
    ],
)
def test_gxx_accepts_only_bounded_single_file_cxx17_build_forms(
    tmp_path: Path,
    argv: list[str],
) -> None:
    source = tmp_path / "main.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")

    command = command_policy.prepare_command(
        tmp_path,
        {"argv": argv},
        environ=_environment(),
    )

    assert command.execution_argv[0] != argv[0]
    assert str(source.resolve()) in command.execution_argv
    assert str((tmp_path / "main.exe").resolve()) in command.execution_argv
    assert command.display_argv[0] == "g++"
    assert "main.cpp" in command.display_argv
    assert "main.exe" in command.display_argv
    assert command.command_kind == "build"


def test_gxx_paths_are_resolved_from_non_root_cwd(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "main.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")

    command = command_policy.prepare_command(
        tmp_path,
        {
            "argv": ["g++", "main.cpp", "-std=c++17", "-o", "main.exe"],
            "cwd": "nested",
        },
        environ=_environment(),
    )

    assert command.execution_argv[-1] == str((nested / "main.exe").resolve())
    assert command.display_argv == (
        "g++",
        "nested/main.cpp",
        "-std=c++17",
        "-o",
        "nested/main.exe",
    )
    assert command.relative_cwd == "nested"


@pytest.mark.parametrize(
    "option",
    [
        "@arguments.rsp",
        "-fplugin=plugin.so",
        "-specs=specs.txt",
        "-wrapper",
        "-Btoolchain",
        "-Iinclude",
        "-include",
        "-Llib",
        "-Wl,-rpath,lib",
        "-Wa,--fatal-warnings",
        "-Wp,-DVALUE=1",
        "-Xlinker",
        "-x",
        "-",
        "--version",
        "-E",
        "-S",
        "-c",
        "-fsyntax-only",
    ],
)
def test_gxx_dangerous_unknown_and_non_build_options_are_blocked(
    tmp_path: Path,
    option: str,
) -> None:
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    _assert_error(
        _prepare(
            tmp_path,
            {
                "argv": [
                    "g++",
                    option,
                    "-std=c++17",
                    "-o",
                    "main.exe",
                    "main.cpp",
                ]
            },
        ),
        "COMMAND_BLOCKED",
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["g++", "main.cpp", "-o", "main.exe"],
        ["g++", "main.cpp", "-std=c++17"],
        ["g++", "-std=c++17", "-o", "main.exe"],
        ["g++", "main.cpp", "-std=c++17", "-o"],
        ["g++", "main.cpp", "-std=c++17", "-o", "one.exe", "-o", "two.exe"],
        ["g++", "main.cpp", "-std=c++17", "-std=c++17", "-o", "main.exe"],
        ["g++", "main.cpp", "-O2", "-O2", "-std=c++17", "-o", "main.exe"],
        ["g++", "main.cpp", "other.cpp", "-std=c++17", "-o", "main.exe"],
        ["g++", "main.cpp", "-std=c++17", "-omain.exe"],
    ],
)
def test_gxx_missing_duplicate_and_extra_arguments_are_rejected(
    tmp_path: Path,
    argv: list[str],
) -> None:
    for name in ("main.cpp", "other.cpp"):
        (tmp_path / name).write_text("int main() { return 0; }\n", encoding="utf-8")

    result = _prepare(tmp_path, {"argv": argv})

    assert isinstance(result, ToolResult)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code in {"COMMAND_BLOCKED", "INVALID_ARGUMENTS"}


@pytest.mark.skipif(os.name != "nt", reason="Windows-only explicit executable suffix rule")
def test_gxx_windows_output_requires_explicit_exe_suffix(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["g++", "main.cpp", "-std=c++17", "-o", "main"]},
        ),
        "COMMAND_BLOCKED",
    )


@pytest.mark.parametrize(
    "source,output",
    [
        ("../outside.cpp", "main.exe"),
        ("main.cpp", "../outside.exe"),
        (".env.cpp", "main.exe"),
        ("main.cpp", ".proofcoder/main.exe"),
        ("missing.cpp", "main.exe"),
        ("main.cpp", "missing/main.exe"),
        ("main.cpp", "main.cpp"),
    ],
)
def test_gxx_source_and_output_paths_enforce_workspace_and_file_rules(
    tmp_path: Path,
    source: str,
    output: str,
) -> None:
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (tmp_path / ".env.cpp").write_text("sensitive\n", encoding="utf-8")

    result = _prepare(
        tmp_path,
        {"argv": ["g++", source, "-std=c++17", "-o", output]},
    )

    assert isinstance(result, ToolResult)
    assert result.ok is False
    assert result.error is not None
    assert result.error.code in {
        "COMMAND_BLOCKED",
        "COMMAND_NOT_FOUND",
        "PATH_OUTSIDE_WORKSPACE",
        "SENSITIVE_PATH",
    }


def test_gxx_rejects_absolute_source_and_output_paths(tmp_path: Path) -> None:
    source = tmp_path / "main.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")

    for argv in (
        ["g++", str(source.resolve()), "-std=c++17", "-o", "main.exe"],
        ["g++", "main.cpp", "-std=c++17", "-o", str((tmp_path / "main.exe").resolve())],
    ):
        _assert_error(_prepare(tmp_path, {"argv": argv}), "PATH_OUTSIDE_WORKSPACE")


def test_gxx_rejects_non_file_source_and_existing_output(tmp_path: Path) -> None:
    (tmp_path / "directory.cpp").mkdir()
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (tmp_path / "main.exe").write_bytes(b"existing")

    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["g++", "directory.cpp", "-std=c++17", "-o", "new.exe"]},
        ),
        "COMMAND_BLOCKED",
    )
    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["g++", "main.cpp", "-std=c++17", "-o", "main.exe"]},
        ),
        "COMMAND_BLOCKED",
    )
    assert (tmp_path / "main.exe").read_bytes() == b"existing"


def test_gxx_rejects_external_source_and_output_parent_symlinks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (workspace / "local.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    try:
        (workspace / "linked.cpp").symlink_to(outside / "main.cpp")
        (workspace / "out").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    _assert_error(
        _prepare(
            workspace,
            {"argv": ["g++", "linked.cpp", "-std=c++17", "-o", "main.exe"]},
        ),
        "PATH_OUTSIDE_WORKSPACE",
    )
    _assert_error(
        _prepare(
            workspace,
            {"argv": ["g++", "local.cpp", "-std=c++17", "-o", "out/main.exe"]},
        ),
        "PATH_OUTSIDE_WORKSPACE",
    )


def test_gxx_rejects_existing_output_symlink(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    target = tmp_path / "target.exe"
    target.write_bytes(b"existing")
    try:
        (tmp_path / "main.exe").symlink_to(target)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["g++", "main.cpp", "-std=c++17", "-o", "main.exe"]},
        ),
        "COMMAND_BLOCKED",
    )
    assert target.read_bytes() == b"existing"


def test_gxx_rejects_explicit_and_workspace_resolved_compilers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compiler_name = "g++.exe" if os.name == "nt" else "g++"
    compiler = tmp_path / compiler_name
    compiler.write_bytes(b"MZ" if os.name == "nt" else b"\x7fELF")
    compiler.chmod(0o755)
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    tail = ["main.cpp", "-std=c++17", "-o", "main.exe"]

    _assert_error(_prepare(tmp_path, {"argv": [f"./{compiler_name}", *tail]}), "COMMAND_BLOCKED")
    monkeypatch.setattr(command_policy.shutil, "which", lambda executable, *, path: str(compiler))
    _assert_error(_prepare(tmp_path, {"argv": ["g++", *tail]}), "COMMAND_BLOCKED")


def test_gxx_rejects_external_link_resolving_to_workspace_compiler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    compiler_name = "g++.exe" if os.name == "nt" else "g++"
    compiler = tmp_path / "workspace-compiler"
    compiler.write_bytes(b"MZ" if os.name == "nt" else b"\x7fELF")
    compiler.chmod(0o755)
    toolchain = tmp_path.parent / f"{tmp_path.name}-linked-toolchain"
    toolchain.mkdir()
    link = toolchain / compiler_name
    try:
        link.symlink_to(compiler)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    monkeypatch.setattr(command_policy.shutil, "which", lambda executable, *, path: str(link))

    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["g++", "main.cpp", "-std=c++17", "-o", "main.exe"]},
        ),
        "COMMAND_BLOCKED",
    )


def test_gxx_rejects_script_and_batch_wrappers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wrapper_name = "g++.cmd" if os.name == "nt" else "g++"
    wrapper_directory = tmp_path.parent / f"{tmp_path.name}-wrapper-toolchain"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / wrapper_name
    if os.name == "nt":
        wrapper.write_text("@echo off\r\n", encoding="utf-8")
    else:
        wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        wrapper.chmod(0o755)
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    monkeypatch.setattr(command_policy.shutil, "which", lambda executable, *, path: str(wrapper))

    _assert_error(
        _prepare(
            tmp_path,
            {"argv": ["g++", "main.cpp", "-std=c++17", "-o", "main.exe"]},
        ),
        "COMMAND_BLOCKED",
    )


@pytest.mark.parametrize("executable", ["c++", "gcc", "clang++", "main.exe"])
def test_other_compilers_and_generated_programs_remain_blocked(
    tmp_path: Path,
    executable: str,
) -> None:
    _assert_error(_prepare(tmp_path, {"argv": [executable]}), "COMMAND_BLOCKED")


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
        CPATH="fake-cpath",
        CPLUS_INCLUDE_PATH="fake-cplus-include-path",
        COMPILER_PATH="fake-compiler-path",
        LIBRARY_PATH="fake-library-path",
        GCC_EXEC_PREFIX="fake-gcc-exec-prefix",
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
    assert "CPATH" not in command.environment
    assert "CPLUS_INCLUDE_PATH" not in command.environment
    assert "COMPILER_PATH" not in command.environment
    assert "LIBRARY_PATH" not in command.environment
    assert "GCC_EXEC_PREFIX" not in command.environment
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
