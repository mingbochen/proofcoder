"""Resolve workspace-relative tool paths without crossing the workspace boundary."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from proofcoder.safety.secrets import is_sensitive_path


class WorkspacePathError(Exception):
    """A stable, non-sensitive workspace path failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_workspace_path(
    workspace: Path,
    requested: str,
    *,
    expected: Literal["directory", "file"],
) -> tuple[Path, str]:
    """Resolve one non-sensitive relative path and verify its expected kind."""

    workspace_root = workspace.resolve(strict=True)
    if _is_absolute_or_drive_qualified(requested):
        raise WorkspacePathError(
            "PATH_OUTSIDE_WORKSPACE",
            "path must be relative to the selected workspace",
        )

    requested_path = Path(requested.replace("\\", "/"))
    resolved = (workspace_root / requested_path).resolve(strict=False)
    ensure_within_workspace(workspace_root, resolved)
    relative = resolved.relative_to(workspace_root).as_posix() or "."
    if is_sensitive_path(requested_path) or is_sensitive_path(relative):
        raise WorkspacePathError(
            "SENSITIVE_PATH",
            "access to sensitive credential or key paths is blocked",
        )
    if not resolved.exists():
        raise WorkspacePathError("PATH_NOT_FOUND", "requested path does not exist")
    if expected == "directory" and not resolved.is_dir():
        raise WorkspacePathError("NOT_A_DIRECTORY", "requested path is not a directory")
    if expected == "file" and not resolved.is_file():
        raise WorkspacePathError("NOT_A_FILE", "requested path is not a regular file")

    return resolved, relative


def resolve_workspace_directory(workspace: Path, requested: str) -> tuple[Path, str]:
    """Resolve a relative directory and return it with its POSIX workspace path."""

    return resolve_workspace_path(workspace, requested, expected="directory")


def resolve_workspace_file(workspace: Path, requested: str) -> tuple[Path, str]:
    """Resolve a relative regular file and return its POSIX workspace path."""

    return resolve_workspace_path(workspace, requested, expected="file")


def resolve_workspace_new_file(workspace: Path, requested: str) -> tuple[Path, str]:
    """Resolve a non-sensitive new-file target without following its final component."""

    workspace_root = workspace.resolve(strict=True)
    if _is_absolute_or_drive_qualified(requested):
        raise WorkspacePathError(
            "PATH_OUTSIDE_WORKSPACE",
            "path must be relative to the selected workspace",
        )

    requested_path = Path(requested.replace("\\", "/"))
    if requested_path.name == "..":
        raise WorkspacePathError(
            "PATH_OUTSIDE_WORKSPACE",
            "path must stay within the selected workspace",
        )
    parent = (workspace_root / requested_path.parent).resolve(strict=False)
    ensure_within_workspace(workspace_root, parent)
    target = parent / requested_path.name
    relative = target.relative_to(workspace_root).as_posix() or "."
    if is_sensitive_path(requested_path) or is_sensitive_path(relative):
        raise WorkspacePathError(
            "SENSITIVE_PATH",
            "access to sensitive credential or key paths is blocked",
        )
    if not parent.exists():
        raise WorkspacePathError("PARENT_NOT_FOUND", "target parent directory does not exist")
    if not parent.is_dir():
        raise WorkspacePathError("NOT_A_DIRECTORY", "target parent path is not a directory")
    if os.path.lexists(target):
        raise WorkspacePathError(
            "PATH_ALREADY_EXISTS",
            "target path already exists and will not be overwritten",
        )

    return target, relative


def _is_absolute_or_drive_qualified(requested: str) -> bool:
    """Recognize native, POSIX, and Windows absolute path forms on every host."""

    native = Path(requested)
    windows = PureWindowsPath(requested)
    posix = PurePosixPath(requested)
    return native.is_absolute() or posix.is_absolute() or bool(windows.drive)


def ensure_within_workspace(workspace_root: Path, candidate: Path) -> None:
    """Reject a resolved path or symlink target outside the workspace."""

    try:
        candidate.resolve(strict=False).relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError):
        raise WorkspacePathError(
            "PATH_OUTSIDE_WORKSPACE",
            "resolved path leaves the selected workspace",
        ) from None
