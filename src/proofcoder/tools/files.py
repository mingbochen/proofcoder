"""Read-only deterministic workspace listing for Stage B."""

from __future__ import annotations

from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path

from proofcoder.safety.paths import (
    WorkspacePathError,
    ensure_within_workspace,
    resolve_workspace_directory,
)
from proofcoder.tools.base import ToolDefinition, ToolResult

MAX_LIST_ENTRIES = 500
_ALWAYS_IGNORED = frozenset(
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
_SENSITIVE_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".key", ".p12", ".pem", ".pfx"})


def create_list_files_tool(workspace: Path) -> ToolDefinition:
    """Create the only Stage B local tool, bound to one workspace."""

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


def is_sensitive_filename(name: str) -> bool:
    """Return whether a filename is always hidden from list_files."""

    lowered = name.casefold()
    if lowered == ".env.example":
        return False
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    return lowered in _SENSITIVE_NAMES or Path(lowered).suffix in _SENSITIVE_SUFFIXES


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
            if entry.name in _ALWAYS_IGNORED or is_sensitive_filename(entry.name):
                continue
            if entry.name.startswith(".") and not include_hidden:
                continue

            ensure_within_workspace(workspace_root, entry)
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
