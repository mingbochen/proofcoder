"""A bounded, read-only map of a workspace: its files and its Python symbols.

The model can already list files and read them one at a time. In a forty-file repository
that means reading files to find out which one to read. This tool answers the question
in between -- what is defined where -- without executing anything: Python sources are
parsed with the standard library's ``ast``, which builds a syntax tree and runs no code.

Only Python gets symbols. Other files are listed by path and size, because a symbol
guessed from another language by pattern matching is worse than no symbol at all: the
model would trust it.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path

from proofcoder.safety.paths import (
    WorkspacePathError,
    ensure_within_workspace,
    resolve_workspace_directory,
)
from proofcoder.safety.secrets import is_sensitive_filename, is_sensitive_path
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.files import DEFAULT_IGNORED_DIRECTORIES

REPOSITORY_MAP_TOOL_NAME = "repository_map"
MAX_MAP_FILES = 400
MAX_SYMBOLS_PER_FILE = 60
MAX_METHODS_PER_CLASS = 40
# A file above this is listed but not parsed. Parsing is bounded work on untrusted
# input, and a single oversized generated module should not dominate a map of the rest.
MAX_PARSE_BYTES = 256 * 1024
DEFAULT_MAP_DEPTH = 6
MAX_MAP_DEPTH = 12


def create_repository_map_tool(workspace: Path) -> ToolDefinition:
    """Create the read-only repository map tool bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _repository_map(workspace_root, arguments)

    return ToolDefinition(
        name=REPOSITORY_MAP_TOOL_NAME,
        description=(
            "Map workspace files and, for Python files, their top-level classes, functions "
            "and class methods with line numbers, parsed without executing any code. Other "
            "languages are listed by path and size only. Use it to find where something is "
            "defined before reading files. Credentials, hidden entries and ignored runtime "
            "directories are omitted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory to map.",
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory depth; direct children are depth 1.",
                    "minimum": 1,
                    "maximum": MAX_MAP_DEPTH,
                    "default": DEFAULT_MAP_DEPTH,
                },
                "include_symbols": {
                    "type": "boolean",
                    "description": "Parse Python files for their symbols.",
                    "default": True,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        execute=execute,
    )


def _repository_map(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    path = str(arguments["path"])
    max_depth = int(arguments["max_depth"])
    include_symbols = bool(arguments["include_symbols"])
    try:
        directory, queried_path = resolve_workspace_directory(workspace_root, path)
        files = _collect_files(workspace_root, directory, max_depth)
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)

    returned = files[:MAX_MAP_FILES]
    entries = [
        _describe(workspace_root, relative, include_symbols=include_symbols)
        for relative in returned
    ]
    truncated_count = len(files) - len(returned)
    return ToolResult.success(
        {
            "queried_path": queried_path,
            "files": entries,
            "returned_count": len(entries),
            "total_file_count": len(files),
            "truncated_count": truncated_count,
            "symbols_parsed": include_symbols,
        },
        truncated=truncated_count > 0,
    )


def _collect_files(workspace_root: Path, directory: Path, max_depth: int) -> list[str]:
    """Walk the tree with exactly the skip rules `list_files` applies."""

    found: list[str] = []

    def visit(current: Path, depth: int) -> None:
        if depth >= max_depth:
            return
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if entry.name.casefold() in DEFAULT_IGNORED_DIRECTORIES or is_sensitive_filename(
                entry.name
            ):
                continue
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                # Never followed, and checked before containment: a link pointing out of
                # the workspace is one entry to skip, not a reason to fail the whole map.
                continue
            ensure_within_workspace(workspace_root, entry)
            resolved_relative = entry.resolve(strict=False).relative_to(workspace_root).as_posix()
            if is_sensitive_path(resolved_relative):
                continue
            if entry.is_dir():
                visit(entry, depth + 1)
            elif entry.is_file():
                found.append(entry.relative_to(workspace_root).as_posix())

    visit(directory, 0)
    return found


def _describe(workspace_root: Path, relative: str, *, include_symbols: bool) -> dict[str, object]:
    path = workspace_root / relative
    try:
        size = path.stat().st_size
    except OSError:
        return {"path": relative, "size": None, "symbols": None, "note": "unreadable"}
    entry: dict[str, object] = {"path": relative, "size": size}
    if not include_symbols or not relative.endswith(".py"):
        return entry
    if size > MAX_PARSE_BYTES:
        entry["note"] = "too large to parse"
        return entry
    symbols, truncated, note = _python_symbols(path)
    if note is not None:
        entry["note"] = note
        return entry
    entry["symbols"] = symbols
    if truncated:
        entry["symbols_truncated"] = truncated
    return entry


def _python_symbols(path: Path) -> tuple[list[dict[str, object]], int, str | None]:
    """Return one file's top-level symbols, how many were cut, or why none were read."""

    try:
        source = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return [], 0, "not UTF-8 text"
    try:
        tree = ast.parse(source, filename=path.name)
    except (SyntaxError, ValueError):
        return [], 0, "does not parse"
    except (RecursionError, MemoryError):
        # Pathologically nested input can exhaust the parser. It is one bad file, and a
        # map of the other thirty-nine must not fail because of it.
        return [], 0, "too deeply nested to parse"

    symbols: list[dict[str, object]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [
                {"name": child.name, "line": child.lineno}
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            symbol: dict[str, object] = {
                "kind": "class",
                "name": node.name,
                "line": node.lineno,
                "methods": methods[:MAX_METHODS_PER_CLASS],
            }
            if len(methods) > MAX_METHODS_PER_CLASS:
                symbol["methods_truncated"] = len(methods) - MAX_METHODS_PER_CLASS
            symbols.append(symbol)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            symbols.append({"kind": "function", "name": node.name, "line": node.lineno})
    truncated = max(0, len(symbols) - MAX_SYMBOLS_PER_FILE)
    return symbols[:MAX_SYMBOLS_PER_FILE], truncated, None
