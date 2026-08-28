"""Bounded, read-only workspace listing and UTF-8 file reading."""

from __future__ import annotations

import codecs
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path

from proofcoder.safety.paths import (
    WorkspacePathError,
    ensure_within_workspace,
    resolve_workspace_directory,
    resolve_workspace_file,
)
from proofcoder.safety.secrets import is_sensitive_filename, is_sensitive_path
from proofcoder.tools.base import ToolDefinition, ToolResult

MAX_LIST_ENTRIES = 500
MAX_FILE_SIZE_BYTES = 1024 * 1024
MAX_READ_LINES = 400
MAX_READ_BYTES = 64 * 1024
_BINARY_SIGNATURES = (
    b"%PDF-",
    b"GIF87a",
    b"GIF89a",
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"\x7fELF",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
)
DEFAULT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".proofcoder",
        ".proofcoder-runs",
    }
)


def create_list_files_tool(workspace: Path) -> ToolDefinition:
    """Create a deterministic directory-listing tool bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _list_files(workspace_root, arguments)

    return ToolDefinition(
        name="list_files",
        description=(
            "List workspace entries without reading file contents. Pattern uses case-sensitive "
            "fnmatch against each workspace-relative POSIX path. Hidden entries require "
            "include_hidden=true; credentials and ignored runtime directories are always omitted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory to list.",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum descendant depth; direct children are depth 1.",
                    "minimum": 0,
                    "maximum": 8,
                    "default": 2,
                },
                "pattern": {
                    "type": ["string", "null"],
                    "description": "Optional case-sensitive fnmatch over workspace-relative paths.",
                    "default": None,
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include ordinary hidden entries, never sensitive files.",
                    "default": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        execute=execute,
    )


def create_read_file_tool(workspace: Path) -> ToolDefinition:
    """Create a bounded UTF-8 file reader bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _read_file(workspace_root, arguments)

    return ToolDefinition(
        name="read_file",
        description=(
            "Read a workspace-relative, non-sensitive UTF-8 text file with 1-based line "
            "numbers. Reads at most 400 lines and 64 KiB per call; use start_line and "
            "end_line to request another segment. Binary and files over 1 MiB are rejected."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file to read.",
                    "minLength": 1,
                },
                "start_line": {
                    "type": "integer",
                    "description": "First 1-based line to return.",
                    "minimum": 1,
                    "default": 1,
                },
                "end_line": {
                    "type": ["integer", "null"],
                    "description": "Optional inclusive 1-based final line.",
                    "minimum": 1,
                    "default": None,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        execute=execute,
    )


def _list_files(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    path = str(arguments["path"])
    max_depth = int(arguments["max_depth"])
    pattern_value = arguments["pattern"]
    pattern = pattern_value if isinstance(pattern_value, str) else None
    include_hidden = bool(arguments["include_hidden"])

    try:
        directory, queried_path = resolve_workspace_directory(workspace_root, path)
        matches = _collect_entries(
            workspace_root=workspace_root,
            directory=directory,
            max_depth=max_depth,
            pattern=pattern,
            include_hidden=include_hidden,
        )
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)

    returned = matches[:MAX_LIST_ENTRIES]
    truncated_count = len(matches) - len(returned)
    return ToolResult.success(
        {
            "entries": returned,
            "returned_count": len(returned),
            "total_matched_count": len(matches),
            "truncated_count": truncated_count,
            "queried_path": queried_path,
        },
        truncated=truncated_count > 0,
    )


def _read_file(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    path = str(arguments["path"])
    start_line = int(arguments["start_line"])
    end_value = arguments["end_line"]
    end_line = end_value if isinstance(end_value, int) and not isinstance(end_value, bool) else None

    if end_line is not None and end_line < start_line:
        return ToolResult.failure(
            "INVALID_RANGE",
            "end_line must be greater than or equal to start_line",
            retryable=True,
        )

    try:
        file_path, relative_path = resolve_workspace_file(workspace_root, path)
        if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
            return ToolResult.failure(
                "FILE_TOO_LARGE",
                f"file exceeds the {MAX_FILE_SIZE_BYTES}-byte read limit",
                retryable=True,
            )
        raw = file_path.read_bytes()
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)

    if len(raw) > MAX_FILE_SIZE_BYTES:
        return ToolResult.failure(
            "FILE_TOO_LARGE",
            f"file exceeds the {MAX_FILE_SIZE_BYTES}-byte read limit",
            retryable=True,
        )
    if _looks_binary(raw):
        return ToolResult.failure(
            "BINARY_FILE",
            "binary files cannot be read as model context",
            retryable=True,
        )

    has_bom = raw.startswith(codecs.BOM_UTF8)
    try:
        text = raw.decode("utf-8-sig" if has_bom else "utf-8")
    except UnicodeDecodeError:
        return ToolResult.failure(
            "DECODE_ERROR",
            "file is not valid UTF-8 text",
            retryable=True,
        )

    lines = text.splitlines()
    total_lines = len(lines)
    newline_style = _detect_newline_style(text)
    encoding = "utf-8-sig" if has_bom else "utf-8"

    if total_lines == 0:
        if start_line != 1:
            return ToolResult.failure(
                "INVALID_RANGE",
                "start_line is beyond the empty file",
                retryable=True,
            )
        return ToolResult.success(
            {
                "path": relative_path,
                "content": "",
                "total_lines": 0,
                "start_line": None,
                "end_line": None,
                "actual_start_line": None,
                "actual_end_line": None,
                "returned_line_count": 0,
                "returned_bytes": 0,
                "encoding": encoding,
                "newline_style": newline_style,
            }
        )

    if start_line > total_lines:
        return ToolResult.failure(
            "INVALID_RANGE",
            f"start_line exceeds the file's {total_lines} lines",
            retryable=True,
        )

    requested_end = total_lines if end_line is None else min(end_line, total_lines)
    line_limited_end = min(requested_end, start_line + MAX_READ_LINES - 1)
    content, actual_end, byte_truncated = _render_numbered_lines(
        lines,
        start_line=start_line,
        end_line=line_limited_end,
    )
    truncated = line_limited_end < requested_end or byte_truncated
    returned_line_count = actual_end - start_line + 1
    return ToolResult.success(
        {
            "path": relative_path,
            "content": content,
            "total_lines": total_lines,
            "start_line": start_line,
            "end_line": actual_end,
            "actual_start_line": start_line,
            "actual_end_line": actual_end,
            "returned_line_count": returned_line_count,
            "returned_bytes": len(content.encode("utf-8")),
            "encoding": encoding,
            "newline_style": newline_style,
        },
        truncated=truncated,
    )


def _looks_binary(raw: bytes) -> bool:
    return b"\x00" in raw or raw.startswith(_BINARY_SIGNATURES)


def _detect_newline_style(text: str) -> str:
    styles: set[str] = set()
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                styles.add("crlf")
                index += 2
                continue
            styles.add("cr")
        elif character == "\n":
            styles.add("lf")
        index += 1
    if not styles:
        return "none"
    if len(styles) > 1:
        return "mixed"
    return next(iter(styles))


def _render_numbered_lines(
    lines: list[str],
    *,
    start_line: int,
    end_line: int,
) -> tuple[str, int, bool]:
    rendered: list[str] = []
    used_bytes = 0
    actual_end = start_line - 1
    byte_truncated = False
    for line_number in range(start_line, end_line + 1):
        separator = "\n" if rendered else ""
        fragment = f"{separator}{line_number}: {lines[line_number - 1]}"
        encoded = fragment.encode("utf-8")
        remaining = MAX_READ_BYTES - used_bytes
        if len(encoded) <= remaining:
            rendered.append(fragment)
            used_bytes += len(encoded)
            actual_end = line_number
            continue

        clipped = encoded[:remaining].decode("utf-8", errors="ignore")
        if clipped:
            rendered.append(clipped)
            actual_end = line_number
        byte_truncated = True
        break
    return "".join(rendered), actual_end, byte_truncated


def _collect_entries(
    *,
    workspace_root: Path,
    directory: Path,
    max_depth: int,
    pattern: str | None,
    include_hidden: bool,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []

    def visit(current: Path, depth: int) -> None:
        if depth >= max_depth:
            return
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if entry.name.casefold() in DEFAULT_IGNORED_DIRECTORIES or is_sensitive_filename(
                entry.name
            ):
                continue
            if entry.name.startswith(".") and not include_hidden:
                continue

            ensure_within_workspace(workspace_root, entry)
            resolved_relative = entry.resolve(strict=False).relative_to(workspace_root).as_posix()
            if is_sensitive_path(resolved_relative):
                continue
            relative = entry.relative_to(workspace_root).as_posix()
            is_symlink = entry.is_symlink()
            if is_symlink:
                entry_type = "symlink"
            elif entry.is_dir():
                entry_type = "directory"
            else:
                entry_type = "file"

            if pattern is None or fnmatchcase(relative, pattern):
                matches.append({"path": relative, "type": entry_type})
            if entry_type == "directory":
                visit(entry, depth + 1)

    visit(directory, 0)
    return matches
