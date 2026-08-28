"""Shared workspace path and sensitive-file safety helpers."""

from proofcoder.safety.paths import (
    WorkspacePathError,
    resolve_workspace_directory,
    resolve_workspace_file,
    resolve_workspace_new_file,
)
from proofcoder.safety.secrets import is_sensitive_filename, is_sensitive_path
from proofcoder.safety.writes import (
    FileSnapshot,
    commit_new_file,
    commit_replacement,
    discard_temporary_file,
    snapshot_still_matches,
    stage_temporary_file,
)

__all__ = [
    "FileSnapshot",
    "WorkspacePathError",
    "commit_new_file",
    "commit_replacement",
    "discard_temporary_file",
    "is_sensitive_filename",
    "is_sensitive_path",
    "resolve_workspace_directory",
    "resolve_workspace_file",
    "resolve_workspace_new_file",
    "snapshot_still_matches",
    "stage_temporary_file",
]
