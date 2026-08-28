"""Shared workspace path and sensitive-file safety helpers."""

from proofcoder.safety.paths import (
    WorkspacePathError,
    is_internal_runtime_path,
    resolve_workspace_argument_path,
    resolve_workspace_directory,
    resolve_workspace_file,
    resolve_workspace_new_file,
)
from proofcoder.safety.secrets import (
    is_safe_token_statistic,
    is_sensitive_environment_name,
    is_sensitive_filename,
    is_sensitive_path,
    minimal_subprocess_environment,
    redact_text,
    sensitive_environment_values,
)
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
    "is_internal_runtime_path",
    "is_safe_token_statistic",
    "is_sensitive_environment_name",
    "is_sensitive_filename",
    "is_sensitive_path",
    "minimal_subprocess_environment",
    "redact_text",
    "resolve_workspace_argument_path",
    "resolve_workspace_directory",
    "resolve_workspace_file",
    "resolve_workspace_new_file",
    "sensitive_environment_values",
    "snapshot_still_matches",
    "stage_temporary_file",
]
