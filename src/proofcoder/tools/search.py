"""Bounded workspace text search with ripgrep and deterministic Python fallback."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import BinaryIO, TypeAlias

from proofcoder.safety.paths import (
    WorkspacePathError,
    ensure_within_workspace,
    resolve_workspace_directory,
)
from proofcoder.safety.secrets import is_sensitive_path, minimal_subprocess_environment
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.command import _finish_readers, _terminate_process_tree
from proofcoder.tools.files import DEFAULT_IGNORED_DIRECTORIES, MAX_FILE_SIZE_BYTES

MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILE_SIZE_BYTES = MAX_FILE_SIZE_BYTES
MAX_MATCH_LINE_CHARS = 500
# Five seconds for the complete optional-backend attempt keeps search responsive while the
# deterministic Python backend remains available for slower or unhealthy installations.
RIPGREP_TIMEOUT_SECONDS = 5.0
MAX_RIPGREP_STDOUT_BYTES = 2 * 1024 * 1024
MAX_RIPGREP_STDERR_BYTES = 64 * 1024
_RIPGREP_READ_CHUNK_BYTES = 64 * 1024
_RIPGREP_POLL_SECONDS = 0.025

RipgrepResolver: TypeAlias = Callable[
    [Path, Mapping[str, str]],
    str | Path | None,
]


@dataclass(frozen=True, slots=True)
class _SearchFile:
    path: Path
    relative_path: str
    text: str


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    matches: list[dict[str, object]]
    more_matches_available: bool


@dataclass(slots=True)
class _BoundedBytes:
    limit: int
    retained: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    def append(self, chunk: bytes) -> bool:
        """Retain at most limit bytes and report whether the stream crossed it."""

        self.total_bytes += len(chunk)
        remaining = max(0, self.limit - len(self.retained))
        if remaining:
            self.retained.extend(chunk[:remaining])
        return self.total_bytes > self.limit


@dataclass(frozen=True, slots=True)
class _RipgrepProcessOutput:
    returncode: int
    stdout: bytes


class _RipgrepBackendError(Exception):
    """Signal an unusable optional backend without retaining its raw output."""


def create_search_text_tool(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    ripgrep_resolver: RipgrepResolver | None = None,
) -> ToolDefinition:
    """Create a text-search tool bound to one resolved workspace."""

    workspace_root = workspace.resolve(strict=True)
    resolver = _resolve_ripgrep_from_path if ripgrep_resolver is None else ripgrep_resolver

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _search_text(
            workspace_root,
            arguments,
            environ=environ,
            ripgrep_resolver=resolver,
        )

    return ToolDefinition(
        name="search_text",
        description=(
            "Search non-sensitive UTF-8 workspace files and return matching lines in sorted "
            "path/line order. query is literal unless regex=true. glob is a case-sensitive "
            "fnmatch pattern over each workspace-relative POSIX path (so '*.py' also matches "
            "nested Python files). Search is capped at 200 results and skips binary, files "
            "over 1 MiB, symlinks, and standard runtime/cache directories."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Required non-empty literal text or regular expression.",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory to search.",
                    "default": ".",
                },
                "glob": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional case-sensitive fnmatch over workspace-relative POSIX paths."
                    ),
                    "default": None,
                },
                "regex": {
                    "type": "boolean",
                    "description": "Interpret query as a Python-compatible regular expression.",
                    "default": False,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Match letter case exactly.",
                    "default": False,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Global result limit from 1 through 200.",
                    "minimum": 1,
                    "maximum": MAX_SEARCH_RESULTS,
                    "default": 50,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        execute=execute,
    )


def _search_text(
    workspace_root: Path,
    arguments: Mapping[str, object],
    *,
    environ: Mapping[str, str] | None,
    ripgrep_resolver: RipgrepResolver,
) -> ToolResult:
    query = str(arguments["query"])
    requested_path = str(arguments["path"])
    glob_value = arguments["glob"]
    glob = glob_value if isinstance(glob_value, str) else None
    regex = bool(arguments["regex"])
    case_sensitive = bool(arguments["case_sensitive"])
    max_results = int(arguments["max_results"])

    compiled_pattern: re.Pattern[str] | None = None
    if regex:
        try:
            compiled_pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error:
            return ToolResult.failure(
                "INVALID_PATTERN",
                "query is not a valid regular expression",
                retryable=True,
            )

    try:
        directory, queried_path = resolve_workspace_directory(workspace_root, requested_path)
        files = _collect_search_files(
            workspace_root=workspace_root,
            directory=directory,
            glob=glob,
        )
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)
    except OSError:
        return ToolResult.failure(
            "SEARCH_ERROR",
            "workspace files could not be enumerated safely",
            retryable=True,
        )

    environment = minimal_subprocess_environment(environ)
    try:
        candidate = ripgrep_resolver(workspace_root, environment)
        ripgrep = _validate_ripgrep_candidate(
            workspace_root,
            candidate,
            environment,
        )
    except (OSError, ValueError):
        ripgrep = None

    try:
        if ripgrep is None:
            outcome = _search_with_python(
                files,
                query=query,
                compiled_pattern=compiled_pattern,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
        else:
            outcome = _search_with_ripgrep(
                workspace_root=workspace_root,
                ripgrep=ripgrep,
                environment=environment,
                files=files,
                query=query,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
    except (_RipgrepBackendError, OSError, UnicodeError, ValueError):
        outcome = _search_with_python(
            files,
            query=query,
            compiled_pattern=compiled_pattern,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )

    outcome.matches.sort(key=lambda match: (str(match["path"]), int(match["line_number"])))
    return ToolResult.success(
        {
            "query": query,
            "queried_path": queried_path,
            "matches": outcome.matches,
            "returned_count": len(outcome.matches),
            "more_matches_available": outcome.more_matches_available,
        },
        truncated=outcome.more_matches_available,
    )


def _collect_search_files(
    *,
    workspace_root: Path,
    directory: Path,
    glob: str | None,
) -> list[_SearchFile]:
    files: list[_SearchFile] = []

    def visit(current: Path) -> None:
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if entry.is_symlink():
                continue
            ensure_within_workspace(workspace_root, entry)
            relative = entry.relative_to(workspace_root).as_posix()
            if is_sensitive_path(relative):
                continue
            if entry.is_dir():
                if entry.name.casefold() not in DEFAULT_IGNORED_DIRECTORIES:
                    visit(entry)
                continue
            if not entry.is_file() or (glob is not None and not fnmatchcase(relative, glob)):
                continue
            if entry.stat().st_size > MAX_SEARCH_FILE_SIZE_BYTES:
                continue
            raw = entry.read_bytes()
            if len(raw) > MAX_SEARCH_FILE_SIZE_BYTES or b"\x00" in raw:
                continue
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            files.append(_SearchFile(path=entry, relative_path=relative, text=text))

    visit(directory)
    files.sort(key=lambda item: item.relative_path)
    return files


def _search_with_python(
    files: list[_SearchFile],
    *,
    query: str,
    compiled_pattern: re.Pattern[str] | None,
    case_sensitive: bool,
    max_results: int,
) -> _SearchOutcome:
    matches: list[dict[str, object]] = []
    comparable_query = query if case_sensitive else query.casefold()
    for file in files:
        for line_number, line in enumerate(file.text.splitlines(), start=1):
            if compiled_pattern is not None:
                matched = compiled_pattern.search(line) is not None
            else:
                comparable_line = line if case_sensitive else line.casefold()
                matched = comparable_query in comparable_line
            if not matched:
                continue
            if len(matches) == max_results:
                return _SearchOutcome(matches=matches, more_matches_available=True)
            matches.append(_match_record(file.relative_path, line_number, line))
    return _SearchOutcome(matches=matches, more_matches_available=False)


def _resolve_ripgrep_from_path(
    workspace_root: Path,
    environment: Mapping[str, str],
) -> Path | None:
    """Find rg only in absolute, external directories explicitly present in PATH."""

    executable_name = "rg.exe" if os.name == "nt" else "rg"
    for lexical_directory, _ in _trusted_path_directories(workspace_root, environment):
        candidate = lexical_directory / executable_name
        if _validate_ripgrep_candidate(workspace_root, candidate, environment) is not None:
            return candidate
    return None


def _validate_ripgrep_candidate(
    workspace_root: Path,
    candidate: str | Path | None,
    environment: Mapping[str, str],
    *,
    platform_name: str | None = None,
) -> str | None:
    """Return one canonical trusted rg path, or reject it without executing it."""

    if candidate is None:
        return None
    platform = os.name if platform_name is None else platform_name
    requested = Path(candidate)
    if not requested.is_absolute():
        return None

    lexical = Path(os.path.abspath(requested))
    workspace = workspace_root.resolve(strict=True)
    if _path_is_within(workspace, lexical, platform_name=platform):
        return None

    expected_name = "rg.exe" if platform == "nt" else "rg"
    if (
        lexical.name.casefold() != expected_name.casefold()
        if platform == "nt"
        else lexical.name != expected_name
    ):
        return None

    trusted_directories = _trusted_path_directories(
        workspace,
        environment,
        platform_name=platform,
    )
    try:
        lexical_parent = lexical.parent.resolve(strict=True)
    except OSError:
        return None
    if not any(
        _paths_equal(lexical.parent, path_directory, platform_name=platform)
        or _paths_equal(lexical_parent, resolved_directory, platform_name=platform)
        for path_directory, resolved_directory in trusted_directories
    ):
        return None

    try:
        resolved = requested.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        return None
    if _path_is_within(workspace, resolved, platform_name=platform):
        return None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    if platform == "nt":
        if resolved.suffix.casefold() != ".exe":
            return None
    elif not os.access(resolved, os.X_OK):
        return None
    return str(resolved)


def _trusted_path_directories(
    workspace_root: Path,
    environment: Mapping[str, str],
    *,
    platform_name: str | None = None,
) -> tuple[tuple[Path, Path], ...]:
    """Return absolute PATH entries whose lexical and resolved forms are external."""

    platform = os.name if platform_name is None else platform_name
    trusted: list[tuple[Path, Path]] = []
    for entry in environment.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        directory = Path(entry)
        if not directory.is_absolute():
            continue
        lexical = Path(os.path.abspath(directory))
        if _path_is_within(workspace_root, lexical, platform_name=platform):
            continue
        try:
            resolved = directory.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir() or _path_is_within(
            workspace_root,
            resolved,
            platform_name=platform,
        ):
            continue
        trusted.append((lexical, resolved))
    return tuple(trusted)


def _path_is_within(root: Path, candidate: Path, *, platform_name: str) -> bool:
    try:
        common = Path(os.path.commonpath((str(root), str(candidate))))
    except ValueError:
        return False
    return _paths_equal(root, common, platform_name=platform_name)


def _paths_equal(first: Path, second: Path, *, platform_name: str) -> bool:
    first_text = os.path.normpath(str(first))
    second_text = os.path.normpath(str(second))
    if platform_name == "nt":
        return first_text.casefold() == second_text.casefold()
    return first_text == second_text


def _search_with_ripgrep(
    *,
    workspace_root: Path,
    ripgrep: str,
    environment: Mapping[str, str],
    files: list[_SearchFile],
    query: str,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
) -> _SearchOutcome:
    matches: list[dict[str, object]] = []
    backend_deadline = time.monotonic() + RIPGREP_TIMEOUT_SECONDS
    for file in files:
        remaining_seconds = backend_deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise _RipgrepBackendError
        remaining_with_probe = max_results - len(matches) + 1
        arguments = [
            ripgrep,
            "--no-config",
            "--json",
            "--line-number",
            "--color=never",
            "--max-count",
            str(remaining_with_probe),
            "--case-sensitive" if case_sensitive else "--ignore-case",
        ]
        if not regex:
            arguments.append("--fixed-strings")
        else:
            arguments.append("--engine=auto")
        arguments.extend(("--", query, file.relative_path))
        completed = _run_ripgrep_process(
            arguments,
            workspace_root=workspace_root,
            environment=environment,
            timeout_seconds=remaining_seconds,
        )
        if completed.returncode not in {0, 1}:
            raise _RipgrepBackendError
        try:
            stdout = completed.stdout.decode("utf-8")
            for event_line in stdout.splitlines():
                event = json.loads(event_line)
                if not isinstance(event, dict) or event.get("type") != "match":
                    continue
                data = event.get("data")
                if not isinstance(data, dict):
                    raise ValueError
                line_number = data.get("line_number")
                lines = data.get("lines")
                if type(line_number) is not int or line_number < 1 or not isinstance(lines, dict):
                    raise ValueError
                line = lines.get("text")
                if not isinstance(line, str):
                    raise ValueError
                if len(matches) == max_results:
                    return _SearchOutcome(matches=matches, more_matches_available=True)
                matches.append(
                    _match_record(
                        file.relative_path,
                        line_number,
                        line.removesuffix("\n").removesuffix("\r"),
                    )
                )
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            raise _RipgrepBackendError from None
    return _SearchOutcome(matches=matches, more_matches_available=False)


def _run_ripgrep_process(
    arguments: list[str],
    *,
    workspace_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> _RipgrepProcessOutput:
    """Run verified rg with finite time and independent raw-byte stream limits."""

    process_options: dict[str, object] = {}
    if os.name == "nt":
        process_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        process_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            arguments,
            cwd=workspace_root,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **process_options,
        )
    except (FileNotFoundError, OSError):
        raise _RipgrepBackendError from None

    if process.stdout is None or process.stderr is None:
        _terminate_process_tree(process, environment)
        raise _RipgrepBackendError

    stdout_capture = _BoundedBytes(MAX_RIPGREP_STDOUT_BYTES)
    stderr_capture = _BoundedBytes(MAX_RIPGREP_STDERR_BYTES)
    abort = threading.Event()
    output_limited = threading.Event()
    reader_failed = threading.Event()
    readers = (
        threading.Thread(
            target=_pump_bounded_stream,
            args=(process.stdout, stdout_capture, abort, output_limited, reader_failed),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_bounded_stream,
            args=(process.stderr, stderr_capture, abort, output_limited, reader_failed),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()

    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if abort.wait(timeout=min(_RIPGREP_POLL_SECONDS, remaining)):
                break
    finally:
        if process.poll() is None:
            _terminate_process_tree(process, environment)
        _finish_readers(process, readers)
        if process.poll() is None:
            _terminate_process_tree(process, environment)

    if (
        timed_out
        or output_limited.is_set()
        or reader_failed.is_set()
        or any(reader.is_alive() for reader in readers)
        or process.returncode is None
    ):
        raise _RipgrepBackendError
    return _RipgrepProcessOutput(
        returncode=process.returncode,
        stdout=bytes(stdout_capture.retained),
    )


def _pump_bounded_stream(
    stream: BinaryIO,
    capture: _BoundedBytes,
    abort: threading.Event,
    output_limited: threading.Event,
    reader_failed: threading.Event,
) -> None:
    try:
        while True:
            chunk = stream.read(_RIPGREP_READ_CHUNK_BYTES)
            if not chunk:
                return
            if capture.append(chunk):
                output_limited.set()
                abort.set()
                return
    except (OSError, ValueError):
        reader_failed.set()
        abort.set()


def _match_record(relative_path: str, line_number: int, line: str) -> dict[str, object]:
    truncated = len(line) > MAX_MATCH_LINE_CHARS
    return {
        "path": relative_path,
        "line_number": line_number,
        "line": line[:MAX_MATCH_LINE_CHARS],
        "line_truncated": truncated,
    }
