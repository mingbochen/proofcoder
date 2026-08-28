"""Bounded workspace text search with ripgrep and deterministic Python fallback."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from proofcoder.safety.paths import (
    WorkspacePathError,
    ensure_within_workspace,
    resolve_workspace_directory,
)
from proofcoder.safety.secrets import is_sensitive_path
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.files import DEFAULT_IGNORED_DIRECTORIES, MAX_FILE_SIZE_BYTES

MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILE_SIZE_BYTES = MAX_FILE_SIZE_BYTES
MAX_MATCH_LINE_CHARS = 500

_ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
_SENSITIVE_ENVIRONMENT_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass(frozen=True, slots=True)
class _SearchFile:
    path: Path
    relative_path: str
    text: str


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    matches: list[dict[str, object]]
    more_matches_available: bool


def create_search_text_tool(workspace: Path) -> ToolDefinition:
    """Create a text-search tool bound to one resolved workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _search_text(workspace_root, arguments)

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


def minimal_subprocess_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return only process essentials, excluding names that resemble credentials."""

    source = os.environ if environ is None else environ
    filtered: dict[str, str] = {}
    for name in source:
        upper_name = name.upper()
        if upper_name not in _ALLOWED_ENVIRONMENT_NAMES:
            continue
        if any(marker in upper_name for marker in _SENSITIVE_ENVIRONMENT_MARKERS):
            continue
        filtered[name] = source[name]
    return filtered


def _search_text(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
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

    ripgrep = shutil.which("rg")
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
                files=files,
                query=query,
                regex=regex,
                case_sensitive=case_sensitive,
                max_results=max_results,
            )
    except FileNotFoundError:
        outcome = _search_with_python(
            files,
            query=query,
            compiled_pattern=compiled_pattern,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
    except _RipgrepPatternError:
        outcome = _search_with_python(
            files,
            query=query,
            compiled_pattern=compiled_pattern,
            case_sensitive=case_sensitive,
            max_results=max_results,
        )
    except (OSError, UnicodeError, ValueError):
        return ToolResult.failure(
            "SEARCH_ERROR",
            "text search failed without exposing subprocess details",
            retryable=True,
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


class _RipgrepPatternError(Exception):
    """Signal a pattern rejected by ripgrep without retaining stderr."""


def _search_with_ripgrep(
    *,
    workspace_root: Path,
    ripgrep: str,
    files: list[_SearchFile],
    query: str,
    regex: bool,
    case_sensitive: bool,
    max_results: int,
) -> _SearchOutcome:
    matches: list[dict[str, object]] = []
    environment = minimal_subprocess_environment()
    for file in files:
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
        completed = subprocess.run(
            arguments,
            cwd=workspace_root,
            env=environment,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if completed.returncode == 2 and regex:
            raise _RipgrepPatternError
        if completed.returncode not in {0, 1}:
            raise OSError("ripgrep search failed")
        if not isinstance(completed.stdout, str):
            raise ValueError("ripgrep output was not text")
        for event_line in completed.stdout.splitlines():
            event = json.loads(event_line)
            if not isinstance(event, dict) or event.get("type") != "match":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                raise ValueError("ripgrep match data was invalid")
            line_number = data.get("line_number")
            lines = data.get("lines")
            if type(line_number) is not int or not isinstance(lines, dict):
                raise ValueError("ripgrep match location was invalid")
            line = lines.get("text")
            if not isinstance(line, str):
                raise ValueError("ripgrep match text was invalid")
            if len(matches) == max_results:
                return _SearchOutcome(matches=matches, more_matches_available=True)
            matches.append(
                _match_record(
                    file.relative_path,
                    line_number,
                    line.removesuffix("\n").removesuffix("\r"),
                )
            )
    return _SearchOutcome(matches=matches, more_matches_available=False)


def _match_record(relative_path: str, line_number: int, line: str) -> dict[str, object]:
    truncated = len(line) > MAX_MATCH_LINE_CHARS
    return {
        "path": relative_path,
        "line_number": line_number,
        "line": line[:MAX_MATCH_LINE_CHARS],
        "line_truncated": truncated,
    }
