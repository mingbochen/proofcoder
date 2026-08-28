"""Shared workspace path and sensitive-file safety helpers."""

from proofcoder.safety.paths import (
    WorkspacePathError,
    resolve_workspace_directory,
    resolve_workspace_file,
)
from proofcoder.safety.secrets import is_sensitive_filename, is_sensitive_path

__all__ = [
    "WorkspacePathError",
    "is_sensitive_filename",
    "is_sensitive_path",
    "resolve_workspace_directory",
    "resolve_workspace_file",
]
