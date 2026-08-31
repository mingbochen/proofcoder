"""Default-deny command policy for bounded local verification commands."""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from proofcoder.safety.paths import (
    WorkspacePathError,
    resolve_workspace_argument_path,
    resolve_workspace_directory,
    resolve_workspace_file,
)
from proofcoder.safety.secrets import (
    minimal_subprocess_environment,
    sensitive_environment_values,
)

MAX_COMMAND_ARGUMENTS = 64
MAX_COMMAND_ARGUMENT_CHARS = 4096
MAX_COMMAND_TOTAL_CHARS = 32 * 1024
MIN_COMMAND_TIMEOUT_SECONDS = 1
MAX_COMMAND_TIMEOUT_SECONDS = 300

_PYTHON_NAME = re.compile(r"python(?:3(?:\.\d+)?)?(?:\.exe)?\Z", re.IGNORECASE)
_PYTHON_ALIASES = frozenset({"py", "py.exe"})
_GXX_EXECUTABLE_NAMES = frozenset({"g++", "g++.exe"})
_KNOWN_EXECUTABLES = frozenset({"git", "pytest", "ruff"})
_FORBIDDEN_EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".ps1", ".sh"})
_BLOCKED_EXECUTABLES = frozenset(
    {
        "apt",
        "apt-get",
        "bash",
        "brew",
        "cargo",
        "chgrp",
        "chmod",
        "chown",
        "cmd",
        "curl",
        "del",
        "dnf",
        "doas",
        "erase",
        "fish",
        "ftp",
        "gem",
        "kill",
        "killall",
        "npm",
        "pacman",
        "pip",
        "pip3",
        "powershell",
        "pwsh",
        "rd",
        "rm",
        "rmdir",
        "runas",
        "scp",
        "sh",
        "ssh",
        "sudo",
        "taskkill",
        "wget",
        "winget",
        "wsl",
        "yum",
        "zsh",
    }
)
_PYTHON_MODULES = frozenset({"compileall", "pytest", "ruff", "unittest"})
_GIT_READ_SUBCOMMANDS = frozenset({"diff", "log", "ls-files", "rev-parse", "show", "status"})
_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "fetch",
        "gc",
        "init",
        "merge",
        "mv",
        "pull",
        "push",
        "rebase",
        "remote",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "submodule",
        "switch",
        "tag",
        "worktree",
    }
)
_GIT_BLOCKED_OPTIONS = frozenset(
    {
        "--config-env",
        "--exec-path",
        "--ext-diff",
        "--git-dir",
        "--no-index",
        "--output",
        "--pathspec-from-file",
        "--textconv",
        "--work-tree",
    }
)
_PYTEST_PATH_OPTIONS = frozenset(
    {
        "--basetemp",
        "--confcutdir",
        "--cov-config",
        "--html",
        "--ignore",
        "--ignore-glob",
        "--json-report-file",
        "--junit-xml",
        "--junitxml",
        "--log-file",
        "--rootdir",
    }
)
_PYTEST_VALUE_OPTIONS = frozenset(
    {
        "--capture",
        "--color",
        "--durations",
        "--durations-min",
        "--import-mode",
        "--lfnf",
        "--maxfail",
        "--tb",
        "-k",
        "-m",
        "-o",
        "-p",
    }
)
_RUFF_PATH_OPTIONS = frozenset({"--config", "--output-file", "--stdin-filename"})
_RUFF_VALUE_OPTIONS = frozenset(
    {
        "--exclude",
        "--extend-exclude",
        "--extension",
        "--line-length",
        "--output-format",
        "--target-version",
        "-e",
    }
)
_RUFF_BLOCKED_OPTIONS = frozenset({"--fix", "--fix-only", "--unsafe-fixes"})


class CommandPolicyError(Exception):
    """A stable command argument or policy failure safe to return to the model."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    """A command that passed policy and is ready for a repeated execution check."""

    execution_argv: tuple[str, ...]
    display_argv: tuple[str, ...]
    cwd: Path
    relative_cwd: str
    timeout_seconds: int
    command_kind: str
    environment: dict[str, str]
    sensitive_values: tuple[str, ...]


def prepare_command(
    workspace: Path,
    arguments: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None = None,
) -> PreparedCommand:
    """Validate, classify, resolve, and normalize one command without side effects."""

    workspace_root = workspace.resolve(strict=True)
    argv = _validated_argv(arguments.get("argv"))
    cwd_value = arguments.get("cwd", ".")
    timeout_value = arguments.get("timeout_seconds", 60)
    if not isinstance(cwd_value, str) or not cwd_value or "\0" in cwd_value:
        raise CommandPolicyError("INVALID_ARGUMENTS", "cwd must be a non-empty string")
    if type(timeout_value) is not int or not (
        MIN_COMMAND_TIMEOUT_SECONDS <= timeout_value <= MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise CommandPolicyError(
            "INVALID_ARGUMENTS",
            f"timeout_seconds must be an integer from {MIN_COMMAND_TIMEOUT_SECONDS} through "
            f"{MAX_COMMAND_TIMEOUT_SECONDS}",
        )

    cwd, relative_cwd = resolve_workspace_directory(workspace_root, cwd_value)
    secrets = sensitive_environment_values(environ)
    if any(secret in item for secret in secrets for item in argv):
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "argv contains a known sensitive environment value",
        )

    environment = _command_environment(environ)
    executable, canonical_name = _resolve_executable(
        workspace_root,
        argv[0],
        environment,
    )
    tail = list(argv[1:])
    if canonical_name == "python":
        command_kind, execution_tail, display_tail = _validate_python(
            workspace_root,
            cwd,
            tail,
        )
    elif canonical_name == "pytest":
        _validate_pytest(workspace_root, cwd, tail)
        command_kind = "test"
        execution_tail = tail
        display_tail = tail
    elif canonical_name == "ruff":
        command_kind = _validate_ruff(workspace_root, cwd, tail)
        execution_tail = tail
        display_tail = tail
    elif canonical_name == "git":
        _validate_git(workspace_root, cwd, tail)
        command_kind = "git_read"
        execution_tail = tail
        display_tail = tail
    elif canonical_name == "g++":
        execution_tail, display_tail = _validate_gxx(workspace_root, cwd, tail)
        command_kind = "build"
    else:
        raise CommandPolicyError("COMMAND_BLOCKED", "executable is not in the command allowlist")

    return PreparedCommand(
        execution_argv=(executable, *execution_tail),
        display_argv=(canonical_name, *display_tail),
        cwd=cwd,
        relative_cwd=relative_cwd,
        timeout_seconds=timeout_value,
        command_kind=command_kind,
        environment=environment,
        sensitive_values=secrets,
    )


def _validated_argv(value: object) -> list[str]:
    if not isinstance(value, list):
        raise CommandPolicyError("INVALID_ARGUMENTS", "argv must be a non-empty string array")
    if not value:
        raise CommandPolicyError("INVALID_ARGUMENTS", "argv must contain an executable")
    if len(value) > MAX_COMMAND_ARGUMENTS:
        raise CommandPolicyError(
            "INVALID_ARGUMENTS",
            f"argv cannot contain more than {MAX_COMMAND_ARGUMENTS} items",
        )

    argv: list[str] = []
    total_characters = 0
    for item in value:
        if not isinstance(item, str) or not item:
            raise CommandPolicyError(
                "INVALID_ARGUMENTS",
                "every argv item must be a non-empty string",
            )
        if "\0" in item:
            raise CommandPolicyError("INVALID_ARGUMENTS", "argv items cannot contain NUL")
        if len(item) > MAX_COMMAND_ARGUMENT_CHARS:
            raise CommandPolicyError(
                "INVALID_ARGUMENTS",
                f"each argv item is limited to {MAX_COMMAND_ARGUMENT_CHARS} characters",
            )
        total_characters += len(item)
        argv.append(item)
    if total_characters > MAX_COMMAND_TOTAL_CHARS:
        raise CommandPolicyError(
            "INVALID_ARGUMENTS",
            f"combined argv text is limited to {MAX_COMMAND_TOTAL_CHARS} characters",
        )
    return argv


def _command_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    environment = minimal_subprocess_environment(environ, command_defaults=True)
    environment["PATH"] = _absolute_path_entries(environment.get("PATH", ""))
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _absolute_path_entries(raw_path: str) -> str:
    accepted: list[str] = []
    seen: set[str] = set()
    for raw_entry in raw_path.split(os.pathsep):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            continue
        normalized = str(candidate)
        comparison = normalized.casefold() if os.name == "nt" else normalized
        if comparison not in seen:
            seen.add(comparison)
            accepted.append(normalized)
    return os.pathsep.join(accepted)


def _resolve_executable(
    workspace: Path,
    requested: str,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    requested_path = Path(requested.replace("\\", "/"))
    if requested_path.suffix.casefold() in _FORBIDDEN_EXECUTABLE_SUFFIXES:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "shell, batch, and PowerShell script executables are blocked",
        )

    explicit_path = "/" in requested or "\\" in requested or bool(PureWindowsPath(requested).drive)
    requested_name = requested_path.name.casefold()
    if explicit_path and requested_name in _GXX_EXECUTABLE_NAMES:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "explicit compiler paths are blocked; use g++ from the sanitized PATH",
        )
    resolved_explicit: Path | None = None
    if explicit_path:
        resolved_explicit, _ = resolve_workspace_file(workspace, requested)
        executable_name = resolved_explicit.name.casefold()
    else:
        executable_name = requested.casefold()

    name_without_exe = executable_name.removesuffix(".exe")
    if _PYTHON_NAME.fullmatch(executable_name) or executable_name in _PYTHON_ALIASES:
        return sys.executable, "python"
    if name_without_exe in _BLOCKED_EXECUTABLES:
        raise CommandPolicyError("COMMAND_BLOCKED", "executable category is blocked by policy")
    if executable_name in _GXX_EXECUTABLE_NAMES:
        return _resolve_gxx_executable(workspace, requested, environment), "g++"
    if name_without_exe not in _KNOWN_EXECUTABLES:
        raise CommandPolicyError("COMMAND_BLOCKED", "executable is not in the command allowlist")

    if resolved_explicit is not None:
        return str(resolved_explicit), name_without_exe
    safe_path = environment.get("PATH", "")
    found = shutil.which(requested, path=safe_path) if safe_path else None
    if found is None:
        raise CommandPolicyError(
            "COMMAND_NOT_FOUND",
            f"allowed executable '{name_without_exe}' was not found on the sanitized PATH",
        )
    resolved = Path(found).resolve(strict=False)
    if not resolved.is_absolute():
        raise CommandPolicyError(
            "COMMAND_NOT_FOUND",
            "allowed executable did not resolve to an absolute PATH entry",
        )
    return str(resolved), name_without_exe


def _resolve_gxx_executable(
    workspace: Path,
    requested: str,
    environment: Mapping[str, str],
) -> str:
    safe_path = environment.get("PATH", "")
    found = shutil.which(requested, path=safe_path) if safe_path else None
    if found is None:
        raise CommandPolicyError(
            "COMMAND_NOT_FOUND",
            "allowed executable 'g++' was not found on the sanitized PATH",
        )

    candidate = Path(found)
    if not candidate.is_absolute():
        raise CommandPolicyError(
            "COMMAND_NOT_FOUND",
            "allowed compiler did not resolve to an absolute PATH entry",
        )
    if (
        candidate.name.casefold() not in _GXX_EXECUTABLE_NAMES
        or candidate.suffix.casefold() in _FORBIDDEN_EXECUTABLE_SUFFIXES
        or (os.name == "nt" and candidate.suffix.casefold() != ".exe")
    ):
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "compiler shell and batch wrappers are blocked",
        )

    lexical_candidate = Path(os.path.abspath(candidate))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise CommandPolicyError(
            "COMMAND_NOT_FOUND",
            "allowed compiler could not be resolved from the sanitized PATH",
        ) from None
    if (
        _is_within_workspace(workspace, lexical_candidate)
        or _is_within_workspace(workspace, resolved)
        or not resolved.is_file()
    ):
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "compiler must be a regular executable installed outside the workspace",
        )
    if resolved.suffix.casefold() in _FORBIDDEN_EXECUTABLE_SUFFIXES:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "compiler shell and batch wrappers are blocked",
        )
    try:
        with resolved.open("rb") as stream:
            if stream.read(2) == b"#!":
                raise CommandPolicyError(
                    "COMMAND_BLOCKED",
                    "compiler script wrappers are blocked",
                )
    except CommandPolicyError:
        raise
    except OSError:
        raise CommandPolicyError(
            "COMMAND_NOT_FOUND",
            "allowed compiler could not be inspected before execution",
        ) from None
    return str(resolved)


def _is_within_workspace(workspace: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return False
    return True


def _validate_gxx(
    workspace: Path,
    cwd: Path,
    arguments: list[str],
) -> tuple[list[str], list[str]]:
    standard_index: int | None = None
    optimization_index: int | None = None
    output_index: int | None = None
    source_index: int | None = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-std=c++17":
            if standard_index is not None:
                raise CommandPolicyError("INVALID_ARGUMENTS", "-std=c++17 may appear only once")
            standard_index = index
        elif argument == "-O2":
            if optimization_index is not None:
                raise CommandPolicyError("INVALID_ARGUMENTS", "-O2 may appear only once")
            optimization_index = index
        elif argument == "-o":
            if output_index is not None:
                raise CommandPolicyError("INVALID_ARGUMENTS", "-o may appear only once")
            index += 1
            if index >= len(arguments):
                raise CommandPolicyError(
                    "INVALID_ARGUMENTS",
                    "-o requires one separate workspace-relative output path",
                )
            output_index = index
        elif argument.startswith(("-", "@")):
            raise CommandPolicyError(
                "COMMAND_BLOCKED",
                "g++ option is outside the restricted single-file C++17 build policy",
            )
        elif source_index is not None:
            raise CommandPolicyError(
                "INVALID_ARGUMENTS",
                "g++ accepts exactly one C++ source file",
            )
        else:
            source_index = index
        index += 1

    if standard_index is None or output_index is None or source_index is None:
        raise CommandPolicyError(
            "INVALID_ARGUMENTS",
            "g++ requires -std=c++17, one .cpp source, and one separate -o output",
        )

    source, relative_source = _validate_gxx_source(
        workspace,
        cwd,
        arguments[source_index],
    )
    output, relative_output = _validate_gxx_output(
        workspace,
        cwd,
        arguments[output_index],
        source=source,
    )
    execution_arguments = list(arguments)
    display_arguments = list(arguments)
    execution_arguments[source_index] = str(source)
    execution_arguments[output_index] = str(output)
    display_arguments[source_index] = relative_source
    display_arguments[output_index] = relative_output
    return execution_arguments, display_arguments


def _validate_gxx_source(workspace: Path, cwd: Path, value: str) -> tuple[Path, str]:
    if value.startswith(("-", "@")) or Path(value.replace("\\", "/")).suffix.casefold() != ".cpp":
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "g++ input must be one workspace-relative .cpp source file",
        )
    try:
        source, relative_source = resolve_workspace_argument_path(workspace, cwd, value)
    except WorkspacePathError as error:
        raise CommandPolicyError(error.code, str(error)) from None
    if not source.exists():
        raise CommandPolicyError("COMMAND_NOT_FOUND", "g++ source file does not exist")
    if not source.is_file():
        raise CommandPolicyError("COMMAND_BLOCKED", "g++ source must be a regular file")
    return source, relative_source


def _validate_gxx_output(
    workspace: Path,
    cwd: Path,
    value: str,
    *,
    source: Path,
) -> tuple[Path, str]:
    if value.startswith(("-", "@")):
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "g++ output must be one workspace-relative file path",
        )
    if os.name == "nt" and Path(value).suffix.casefold() != ".exe":
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "g++ output must use an explicit .exe suffix on Windows",
        )
    try:
        output, relative_output = resolve_workspace_argument_path(workspace, cwd, value)
    except WorkspacePathError as error:
        raise CommandPolicyError(error.code, str(error)) from None

    lexical_output = cwd / Path(value.replace("\\", "/"))
    if os.path.lexists(lexical_output):
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "g++ output already exists and will not be overwritten",
        )
    if not output.parent.exists() or not output.parent.is_dir():
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "g++ output parent directory must already exist",
        )
    if output == source:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "g++ output cannot be the source file",
        )
    return output, relative_output


def _validate_python(
    workspace: Path,
    cwd: Path,
    arguments: list[str],
) -> tuple[str, list[str], list[str]]:
    if not arguments:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "Python must run an allowed module or a workspace .py script",
        )
    if arguments[0] in {"-", "-c"}:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "Python inline code and stdin execution are blocked",
        )
    if arguments[0] == "-m":
        if len(arguments) < 2:
            raise CommandPolicyError("INVALID_ARGUMENTS", "python -m requires a module name")
        module = arguments[1].casefold()
        if module not in _PYTHON_MODULES:
            raise CommandPolicyError(
                "COMMAND_BLOCKED",
                "Python module is not in the command allowlist",
            )
        remaining = arguments[2:]
        if module == "pytest":
            _validate_pytest(workspace, cwd, remaining)
            kind = "test"
        elif module == "unittest":
            _validate_unittest(workspace, cwd, remaining)
            kind = "test"
        elif module == "compileall":
            _validate_compileall(workspace, cwd, remaining)
            kind = "build"
        else:
            kind = _validate_ruff(workspace, cwd, remaining)
        normalized = ["-m", module, *remaining]
        return kind, normalized, normalized
    if arguments[0].startswith("-") or not arguments[0].casefold().endswith(".py"):
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "Python must run an allowed module or a workspace .py script",
        )

    script, relative_script = resolve_workspace_argument_path(workspace, cwd, arguments[0])
    if not script.exists():
        raise CommandPolicyError("COMMAND_NOT_FOUND", "workspace Python script does not exist")
    if not script.is_file():
        raise CommandPolicyError("COMMAND_BLOCKED", "workspace Python script is not a file")
    return (
        "script",
        [str(script), *arguments[1:]],
        [relative_script, *arguments[1:]],
    )


def _validate_pytest(workspace: Path, cwd: Path, arguments: Sequence[str]) -> None:
    _validate_path_options(
        workspace,
        cwd,
        arguments,
        path_options=_PYTEST_PATH_OPTIONS,
        value_options=_PYTEST_VALUE_OPTIONS,
        reject_workspace_root_options=frozenset({"--basetemp"}),
        special_handler=_validate_pytest_special_option,
    )


def _validate_pytest_special_option(
    workspace: Path,
    cwd: Path,
    option: str,
    value: str,
) -> bool:
    if option == "--cov-report":
        report_type, separator, destination = value.partition(":")
        if separator and report_type in {"html", "json", "lcov", "xml"}:
            _validate_path_value(workspace, cwd, destination, option=option)
        return True
    if option == "--cov":
        if _looks_path_like(value):
            _validate_path_value(workspace, cwd, value, option=option)
        return True
    return False


def _validate_unittest(workspace: Path, cwd: Path, arguments: Sequence[str]) -> None:
    _validate_path_options(
        workspace,
        cwd,
        arguments,
        path_options=frozenset({"--start-directory", "--top-level-directory", "-s", "-t"}),
        value_options=frozenset({"--pattern", "-k", "-p"}),
    )


def _validate_compileall(workspace: Path, cwd: Path, arguments: Sequence[str]) -> None:
    _validate_path_options(
        workspace,
        cwd,
        arguments,
        path_options=frozenset({"-d", "-e", "-i", "-p", "-s"}),
        value_options=frozenset({"--invalidation-mode", "-j", "-o", "-x"}),
    )


def _validate_ruff(workspace: Path, cwd: Path, arguments: Sequence[str]) -> str:
    if not arguments:
        raise CommandPolicyError("COMMAND_BLOCKED", "ruff requires check or format --check")
    subcommand = arguments[0].casefold()
    if subcommand not in {"check", "format"}:
        raise CommandPolicyError("COMMAND_BLOCKED", "ruff subcommand is not allowed")
    for argument in arguments[1:]:
        if _option_name(argument) in _RUFF_BLOCKED_OPTIONS:
            raise CommandPolicyError("COMMAND_BLOCKED", "ruff mutation flags are blocked")
    if subcommand == "format" and "--check" not in arguments[1:]:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            "ruff format is allowed only with --check",
        )
    _validate_path_options(
        workspace,
        cwd,
        arguments[1:],
        path_options=_RUFF_PATH_OPTIONS,
        value_options=_RUFF_VALUE_OPTIONS,
    )
    return "static_check"


def _validate_git(workspace: Path, cwd: Path, arguments: Sequence[str]) -> None:
    if not arguments:
        raise CommandPolicyError("INVALID_ARGUMENTS", "git requires a read-only subcommand")
    subcommand = arguments[0].casefold()
    if subcommand in _GIT_WRITE_SUBCOMMANDS:
        raise CommandPolicyError("COMMAND_BLOCKED", "Git write and network operations are blocked")
    if subcommand not in _GIT_READ_SUBCOMMANDS:
        raise CommandPolicyError("COMMAND_BLOCKED", "Git subcommand is not in the read allowlist")

    after_separator = False
    for argument in arguments[1:]:
        if argument == "--":
            after_separator = True
            continue
        option = _option_name(argument)
        if argument == "-C" or argument.startswith("-C") or argument == "-c":
            raise CommandPolicyError(
                "COMMAND_BLOCKED",
                "Git scope/configuration options are blocked",
            )
        if option in _GIT_BLOCKED_OPTIONS:
            raise CommandPolicyError(
                "COMMAND_BLOCKED",
                "Git scope, output, or external-program option is blocked",
            )
        if after_separator or (not argument.startswith("-") and _looks_path_like(argument)):
            _validate_path_value(workspace, cwd, argument, option="git path")


def _validate_path_options(
    workspace: Path,
    cwd: Path,
    arguments: Sequence[str],
    *,
    path_options: frozenset[str],
    value_options: frozenset[str],
    reject_workspace_root_options: frozenset[str] = frozenset(),
    special_handler=None,
) -> None:
    index = 0
    positional_only = False
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            positional_only = True
            index += 1
            continue
        if not positional_only and argument.startswith("-"):
            option, separator, inline_value = argument.partition("=")
            if option in path_options:
                if separator:
                    value = inline_value
                else:
                    index += 1
                    if index >= len(arguments):
                        raise CommandPolicyError(
                            "INVALID_ARGUMENTS",
                            f"{option} requires a workspace-relative path",
                        )
                    value = arguments[index]
                resolved = _validate_path_value(workspace, cwd, value, option=option)
                if option in reject_workspace_root_options and resolved == workspace.resolve(
                    strict=True
                ):
                    raise CommandPolicyError(
                        "COMMAND_BLOCKED",
                        f"{option} cannot target the workspace root",
                    )
            elif special_handler is not None and separator:
                special_handler(workspace, cwd, option, inline_value)
            elif option in value_options and not separator:
                index += 1
                if index >= len(arguments):
                    raise CommandPolicyError(
                        "INVALID_ARGUMENTS",
                        f"{option} requires a value",
                    )
            index += 1
            continue
        value = argument[1:] if argument.startswith("@") else argument
        _validate_path_value(workspace, cwd, value, option="command path")
        index += 1


def _validate_path_value(workspace: Path, cwd: Path, value: str, *, option: str) -> Path:
    if not value or value == "-":
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            f"{option} must use a workspace-relative path, not stdin",
        )
    path_part = value.split("::", 1)[0]
    try:
        resolved, _ = resolve_workspace_argument_path(workspace, cwd, path_part)
    except WorkspacePathError:
        raise CommandPolicyError(
            "COMMAND_BLOCKED",
            f"{option} path is outside the allowed workspace",
        ) from None
    return resolved


def _looks_path_like(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
        or value.startswith("@")
        or bool(PureWindowsPath(value).drive)
    )


def _option_name(argument: str) -> str:
    return argument.split("=", 1)[0]
