"""Resolve list_files directories without leaving the selected workspace."""

from __future__ import annotations

from pathlib import Path


class WorkspacePathError(Exception):
    """A stable, non-sensitive workspace path failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_workspace_directory(workspace: Path, requested: str) -> tuple[Path, str]:
    """Resolve a relative directory and return it with its POSIX workspace path."""

    workspace_root = workspace.resolve(strict=True)
    requested_path = Path(requested)
    if requested_path.is_absolute():
        raise WorkspacePathError(
            "PATH_OUTSIDE_WORKSPACE",
            "path must be relative to the selected workspace",
        )

    resolved = (workspace_root / requested_path).resolve(strict=False)
    ensure_within_workspace(workspace_root, resolved)
    if not resolved.exists():
        raise WorkspacePathError("PATH_NOT_FOUND", "requested path does not exist")
    if not resolved.is_dir():
        raise WorkspacePathError("NOT_A_DIRECTORY", "requested path is not a directory")

    relative = resolved.relative_to(workspace_root).as_posix()
    return resolved, relative or "."


def ensure_within_workspace(workspace_root: Path, candidate: Path) -> None:
    """Reject a resolved path or symlink target outside the workspace."""

    try:
        candidate.resolve(strict=False).relative_to(workspace_root.resolve(strict=True))
    except (OSError, ValueError):
        raise WorkspacePathError(
            "PATH_OUTSIDE_WORKSPACE",
            "resolved path leaves the selected workspace",
        ) from None
