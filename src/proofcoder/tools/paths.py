"""Path-level workspace tools: deleting, moving, and creating directories.

These are the destructive half of the Stage G file tools. Specification section
10.5.6 and ADR-0005 set their boundaries, and two of them matter enough to repeat
here:

* Deletion takes one file or one empty directory and never recurses. Rollback's
  coverage gaps -- oversized files, credential paths, ignored directories -- sit
  exactly where a subtree is most likely to hide something it cannot restore.
* Deleting and moving are refused outright when the run has no checkpoint, because
  ADR-0003 made rollback the precondition for widening write capability. Whether
  they run is decided by the run, never by an argument the model supplies.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from proofcoder.safety.paths import (
    WorkspacePathError,
    resolve_workspace_existing_path,
    resolve_workspace_new_directory,
    resolve_workspace_new_file,
)
from proofcoder.tools.base import RiskLevel, ToolDefinition, ToolResult

NO_CHECKPOINT_CODE = "CHECKPOINT_REQUIRED"
NO_CHECKPOINT_MESSAGE = (
    "this run has no checkpoint, so changes that destroy content cannot be undone; "
    "start the run without --no-checkpoint to use this tool"
)


def create_delete_path_tool(workspace: Path, *, checkpoint_available: bool) -> ToolDefinition:
    """Create a single-entry deletion tool bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        if not checkpoint_available:
            return ToolResult.failure(NO_CHECKPOINT_CODE, NO_CHECKPOINT_MESSAGE, retryable=False)
        return _delete_path(workspace_root, arguments)

    return ToolDefinition(
        name="delete_path",
        description=(
            "Delete one workspace file, one empty directory, or one symbolic link. "
            "Directories with any content are refused: remove their entries first, one "
            "call each. Credential paths and runtime state are never deleted. A symbolic "
            "link is removed without touching whatever it points at."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to delete.",
                    "minLength": 1,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        execute=execute,
        modifies_workspace=True,
        risk_level=RiskLevel.WRITE,
    )


def create_move_path_tool(workspace: Path, *, checkpoint_available: bool) -> ToolDefinition:
    """Create a move/rename tool that never overwrites its destination."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        if not checkpoint_available:
            return ToolResult.failure(NO_CHECKPOINT_CODE, NO_CHECKPOINT_MESSAGE, retryable=False)
        return _move_path(workspace_root, arguments)

    return ToolDefinition(
        name="move_path",
        description=(
            "Move or rename one workspace file or directory. The destination must not "
            "exist and its parent directory must: nothing is ever overwritten. To replace "
            "an existing path, delete it first so both steps appear in the trace."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Workspace-relative existing file or directory to move.",
                    "minLength": 1,
                },
                "destination": {
                    "type": "string",
                    "description": "Workspace-relative path that must not already exist.",
                    "minLength": 1,
                },
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
        execute=execute,
        modifies_workspace=True,
        risk_level=RiskLevel.WRITE,
    )


def create_make_directory_tool(workspace: Path) -> ToolDefinition:
    """Create a directory-creation tool that also creates missing parents."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _make_directory(workspace_root, arguments)

    return ToolDefinition(
        name="make_directory",
        description=(
            "Create one workspace directory, including any missing parent directories. "
            "Succeeds without change when the directory already exists, and is refused "
            "when the path exists as a file or a symbolic link."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative directory to create.",
                    "minLength": 1,
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        execute=execute,
        modifies_workspace=True,
        risk_level=RiskLevel.WRITE,
    )


def _delete_path(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    try:
        target, relative_path = resolve_workspace_existing_path(
            workspace_root, str(arguments["path"])
        )
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)

    is_link = target.is_symlink()
    try:
        if is_link:
            kind = "symlink"
            size = 0
            target.unlink()
        elif target.is_dir():
            kind = "directory"
            size = 0
            if any(target.iterdir()):
                return ToolResult.failure(
                    "DIRECTORY_NOT_EMPTY",
                    "directory is not empty; delete its entries individually first",
                    retryable=True,
                )
            target.rmdir()
        elif target.is_file():
            kind = "file"
            size = target.stat().st_size
            target.unlink()
        else:
            return ToolResult.failure(
                "NOT_A_FILE",
                "only regular files, empty directories, and symbolic links can be deleted",
                retryable=True,
            )
    except OSError:
        return ToolResult.failure(
            "DELETE_ERROR",
            "path could not be deleted; it may be in use or protected",
            retryable=True,
        )

    return ToolResult.success({"path": relative_path, "kind": kind, "bytes_freed": size})


def _move_path(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    try:
        source, relative_source = resolve_workspace_existing_path(
            workspace_root, str(arguments["source"])
        )
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)
    if source.is_symlink():
        return ToolResult.failure(
            "NOT_A_FILE",
            "symbolic links are not moved; delete and recreate them instead",
            retryable=True,
        )

    try:
        destination, relative_destination = resolve_workspace_new_file(
            workspace_root, str(arguments["destination"])
        )
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)
    kind = "directory" if source.is_dir() else "file"
    try:
        # os.rename refuses an existing destination on POSIX for directories only, so the
        # check above is what makes this safe; link-then-unlink would not work for one.
        if os.path.lexists(destination):
            return ToolResult.failure(
                "PATH_ALREADY_EXISTS",
                "destination path already exists and will not be overwritten",
                retryable=True,
            )
        os.rename(source, destination)
    except OSError:
        return ToolResult.failure(
            "MOVE_ERROR",
            "path could not be moved; the destination may be on another filesystem",
            retryable=True,
        )

    return ToolResult.success(
        {"source": relative_source, "destination": relative_destination, "kind": kind}
    )


def _make_directory(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    try:
        target, relative_path = resolve_workspace_new_directory(
            workspace_root, str(arguments["path"])
        )
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)

    if target.is_symlink() or (target.exists() and not target.is_dir()):
        return ToolResult.failure(
            "PATH_ALREADY_EXISTS",
            "path already exists and is not a directory",
            retryable=True,
        )
    if target.is_dir():
        return ToolResult.success({"path": relative_path, "created": False})

    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        # Something appeared here while the checks above were running.
        if target.is_dir() and not target.is_symlink():
            return ToolResult.success({"path": relative_path, "created": False})
        return ToolResult.failure(
            "PATH_ALREADY_EXISTS",
            "path already exists and is not a directory",
            retryable=True,
        )
    except OSError:
        return ToolResult.failure(
            "DIRECTORY_CREATE_ERROR",
            "directory could not be created inside the workspace",
            retryable=True,
        )
    return ToolResult.success({"path": relative_path, "created": True})
