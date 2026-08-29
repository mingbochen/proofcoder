"""Strict loading and workspace-only materialization for offline evaluation fixtures."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from proofcoder.errors import ProofCoderError
from proofcoder.safety.paths import is_internal_runtime_path
from proofcoder.safety.secrets import is_sensitive_path

FIXTURE_SCHEMA_VERSION = 1
MAX_METADATA_BYTES = 64 * 1024
MAX_WORKSPACE_FILE_BYTES = 256 * 1024
MAX_WORKSPACE_BYTES = 1024 * 1024

_FIXTURE_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_METADATA_FIELDS = frozenset(
    {
        "allowed_modified_files",
        "category",
        "id",
        "required_modified_files",
        "schema_version",
        "task",
        "validation",
    }
)
_VALIDATION_FIELDS = frozenset(
    {"argv", "cwd", "initial_exit_code", "initial_output_contains", "success_exit_code"}
)


class FixtureCategory(StrEnum):
    """The three Stage E evaluation task classes."""

    BUG_FIX = "bug_fix"
    FEATURE_ADDITION = "feature_addition"
    CROSS_FILE_CHANGE = "cross_file_change"


class EvalFixtureError(ProofCoderError):
    """A stable, non-sensitive fixture validation or materialization failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FixtureValidation:
    """Local success criterion plus expected initial failure evidence."""

    argv: tuple[str, ...]
    cwd: str
    success_exit_code: int
    initial_exit_code: int
    initial_output_contains: str


@dataclass(frozen=True, slots=True)
class EvalFixture:
    """Validated fixture metadata plus its internal source workspace."""

    fixture_id: str
    category: FixtureCategory
    task: str
    validation: FixtureValidation
    allowed_modified_files: tuple[str, ...]
    required_modified_files: tuple[str, ...]
    workspace_files: tuple[str, ...]
    source_workspace: Path


def load_fixtures(fixtures_root: Path) -> tuple[EvalFixture, ...]:
    """Load every fixture below *fixtures_root* in deterministic ID order."""

    root = _existing_directory(fixtures_root, "FIXTURE_ROOT_INVALID")
    try:
        entries = sorted(root.iterdir(), key=lambda path: (path.name.casefold(), path.name))
    except OSError:
        raise EvalFixtureError(
            "FIXTURE_ROOT_INVALID", "fixture root could not be listed safely"
        ) from None
    if not entries:
        raise EvalFixtureError("FIXTURE_ROOT_INVALID", "fixture root must not be empty")

    fixtures: list[EvalFixture] = []
    identifiers: set[str] = set()
    for entry in entries:
        if entry.is_symlink():
            raise EvalFixtureError("FIXTURE_SYMLINK", "fixture directories must not be symlinks")
        if not entry.is_dir():
            raise EvalFixtureError(
                "FIXTURE_LAYOUT_INVALID", "fixture root may contain only fixture directories"
            )
        fixture = _load_fixture(entry)
        if fixture.fixture_id in identifiers:
            raise EvalFixtureError("DUPLICATE_FIXTURE_ID", "fixture IDs must be unique")
        identifiers.add(fixture.fixture_id)
        fixtures.append(fixture)
    return tuple(sorted(fixtures, key=lambda fixture: fixture.fixture_id))


def materialize_fixture(fixture: EvalFixture, destination: Path) -> tuple[str, ...]:
    """Copy only validated workspace content into an empty destination."""

    directories, workspace_files = _inspect_workspace(fixture.source_workspace)
    if workspace_files != fixture.workspace_files:
        raise EvalFixtureError(
            "FIXTURE_CHANGED", "fixture workspace changed after metadata validation"
        )

    contents: list[tuple[str, bytes]] = []
    for relative in workspace_files:
        source = fixture.source_workspace.joinpath(*PurePosixPath(relative).parts)
        if source.is_symlink() or not source.is_file():
            raise EvalFixtureError(
                "FIXTURE_CHANGED", "fixture workspace changed before materialization"
            )
        try:
            content = source.read_bytes()
        except OSError:
            raise EvalFixtureError(
                "FIXTURE_CHANGED", "fixture workspace could not be read safely"
            ) from None
        contents.append((relative, content))

    target = _prepare_destination(destination)
    try:
        for relative in directories:
            target.joinpath(*PurePosixPath(relative).parts).mkdir()
        for relative, content in contents:
            output = target.joinpath(*PurePosixPath(relative).parts)
            with output.open("xb") as stream:
                stream.write(content)
    except OSError:
        raise EvalFixtureError(
            "TARGET_WRITE_FAILED", "fixture workspace could not be materialized safely"
        ) from None
    return workspace_files


def _load_fixture(directory: Path) -> EvalFixture:
    try:
        entries = {entry.name: entry for entry in directory.iterdir()}
    except OSError:
        raise EvalFixtureError(
            "FIXTURE_LAYOUT_INVALID", "fixture directory could not be listed safely"
        ) from None
    if set(entries) != {"fixture.json", "workspace"}:
        raise EvalFixtureError(
            "FIXTURE_LAYOUT_INVALID",
            "each fixture must contain only fixture.json and workspace",
        )

    metadata_path = entries["fixture.json"]
    workspace = entries["workspace"]
    if metadata_path.is_symlink() or workspace.is_symlink():
        raise EvalFixtureError("FIXTURE_SYMLINK", "fixture internals must not be symlinks")
    if not metadata_path.is_file() or not workspace.is_dir():
        raise EvalFixtureError(
            "FIXTURE_LAYOUT_INVALID", "fixture.json and workspace have invalid types"
        )

    metadata = _read_metadata(metadata_path)
    fixture_id = _required_string(metadata["id"], "id", limit=64)
    if _FIXTURE_ID_PATTERN.fullmatch(fixture_id) is None:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "fixture ID must be a lowercase stable identifier"
        )
    category_value = _required_string(metadata["category"], "category", limit=32)
    try:
        category = FixtureCategory(category_value)
    except ValueError:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "fixture category is not supported"
        ) from None
    task = _required_string(metadata["task"], "task", limit=2000)
    validation = _parse_validation(metadata["validation"])
    allowed = _path_list(metadata["allowed_modified_files"], "allowed_modified_files")
    required = _path_list(metadata["required_modified_files"], "required_modified_files")
    if not {path.casefold() for path in required} <= {path.casefold() for path in allowed}:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "required modified files must be allowed"
        )

    directories, workspace_files = _inspect_workspace(workspace)
    if not workspace_files:
        raise EvalFixtureError(
            "FIXTURE_LAYOUT_INVALID", "fixture workspace must contain at least one file"
        )
    if validation.cwd != "." and validation.cwd not in directories:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "validation cwd must name a workspace directory"
        )
    return EvalFixture(
        fixture_id=fixture_id,
        category=category,
        task=task,
        validation=validation,
        allowed_modified_files=allowed,
        required_modified_files=required,
        workspace_files=workspace_files,
        source_workspace=workspace,
    )


def _read_metadata(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "fixture metadata could not be read safely"
        ) from None
    if len(raw) > MAX_METADATA_BYTES:
        raise EvalFixtureError("FIXTURE_METADATA_INVALID", "fixture metadata is too large")
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "fixture metadata must be valid UTF-8 JSON"
        ) from None
    metadata = _mapping_with_fields(value, _METADATA_FIELDS)
    if (
        type(metadata["schema_version"]) is not int
        or metadata["schema_version"] != FIXTURE_SCHEMA_VERSION
    ):
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "fixture schema version is not supported"
        )
    return metadata


def _parse_validation(value: object) -> FixtureValidation:
    validation = _mapping_with_fields(value, _VALIDATION_FIELDS)
    argv_value = validation["argv"]
    if not isinstance(argv_value, list) or not 1 <= len(argv_value) <= 32:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "validation argv must be a non-empty string list"
        )
    argv: list[str] = []
    for argument in argv_value:
        if (
            not isinstance(argument, str)
            or not argument
            or argument != argument.strip()
            or "\x00" in argument
            or len(argument) > 256
        ):
            raise EvalFixtureError(
                "FIXTURE_METADATA_INVALID", "validation argv contains an invalid argument"
            )
        argv.append(argument)

    cwd = _relative_path(validation["cwd"], allow_root=True)
    success_exit_code = validation["success_exit_code"]
    if type(success_exit_code) is not int or success_exit_code != 0:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "success exit code must be the integer zero"
        )
    initial_exit_code = validation["initial_exit_code"]
    if type(initial_exit_code) is not int or initial_exit_code == success_exit_code:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "expected initial exit code must be a non-zero integer"
        )
    initial_output = _required_string(
        validation["initial_output_contains"], "initial_output_contains", limit=500
    )
    return FixtureValidation(
        argv=tuple(argv),
        cwd=cwd,
        success_exit_code=success_exit_code,
        initial_exit_code=initial_exit_code,
        initial_output_contains=initial_output,
    )


def _mapping_with_fields(value: object, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", "fixture metadata fields do not match the schema"
        )
    return value


def _required_string(value: object, field: str, *, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > limit
    ):
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", f"fixture {field} must be a non-empty bounded string"
        )
    return value


def _path_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvalFixtureError(
            "FIXTURE_METADATA_INVALID", f"fixture {field} must be a non-empty path list"
        )
    paths = tuple(_relative_path(item) for item in value)
    if len({path.casefold() for path in paths}) != len(paths):
        raise EvalFixtureError("DUPLICATE_FIXTURE_PATH", "fixture paths must be unique")
    return paths


def _relative_path(value: object, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 240:
        raise EvalFixtureError("UNSAFE_FIXTURE_PATH", "fixture paths must be bounded strings")
    if allow_root and value == ".":
        return value
    if "\\" in value:
        raise EvalFixtureError(
            "UNSAFE_FIXTURE_PATH", "fixture paths must use canonical relative POSIX form"
        )
    parts = value.split("/")
    windows = PureWindowsPath(value)
    if (
        PurePosixPath(value).is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(value).as_posix() != value
    ):
        raise EvalFixtureError(
            "UNSAFE_FIXTURE_PATH", "fixture paths must stay relative without traversal"
        )
    if is_sensitive_path(value) or is_internal_runtime_path(value):
        raise EvalFixtureError(
            "SENSITIVE_FIXTURE_PATH", "sensitive fixture paths are not permitted"
        )
    return value


def _inspect_workspace(workspace: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    root = _existing_directory(workspace, "FIXTURE_LAYOUT_INVALID")
    directories: list[str] = []
    files: list[str] = []
    seen_paths: set[str] = set()
    total_bytes = 0

    def visit(directory: Path, prefix: str) -> None:
        nonlocal total_bytes
        try:
            children = sorted(
                directory.iterdir(), key=lambda path: (path.name.casefold(), path.name)
            )
        except OSError:
            raise EvalFixtureError(
                "FIXTURE_LAYOUT_INVALID", "fixture workspace could not be listed safely"
            ) from None
        for child in children:
            relative = child.name if not prefix else f"{prefix}/{child.name}"
            if child.is_symlink():
                raise EvalFixtureError(
                    "FIXTURE_SYMLINK", "fixture workspaces must not contain symlinks"
                )
            safe_relative = _relative_path(relative)
            folded = safe_relative.casefold()
            if folded in seen_paths:
                raise EvalFixtureError(
                    "DUPLICATE_FIXTURE_PATH", "fixture workspace paths must be unique"
                )
            seen_paths.add(folded)
            if child.is_dir():
                directories.append(safe_relative)
                visit(child, safe_relative)
            elif child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    raise EvalFixtureError(
                        "FIXTURE_LAYOUT_INVALID", "fixture file could not be inspected safely"
                    ) from None
                if size > MAX_WORKSPACE_FILE_BYTES:
                    raise EvalFixtureError(
                        "FIXTURE_LAYOUT_INVALID", "fixture workspace file is too large"
                    )
                total_bytes += size
                if total_bytes > MAX_WORKSPACE_BYTES:
                    raise EvalFixtureError(
                        "FIXTURE_LAYOUT_INVALID", "fixture workspace is too large"
                    )
                files.append(safe_relative)
            else:
                raise EvalFixtureError(
                    "FIXTURE_LAYOUT_INVALID",
                    "fixture workspace entries must be files or directories",
                )

    visit(root, "")
    return tuple(directories), tuple(files)


def _existing_directory(path: Path, code: str) -> Path:
    if path.is_symlink():
        raise EvalFixtureError("FIXTURE_SYMLINK", "fixture directories must not be symlinks")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise EvalFixtureError(code, "required fixture directory does not exist") from None
    if not resolved.is_dir():
        raise EvalFixtureError(code, "required fixture path is not a directory")
    return resolved


def _prepare_destination(destination: Path) -> Path:
    if destination.is_symlink():
        raise EvalFixtureError("TARGET_UNSAFE", "materialization target must not be a symlink")
    if os.path.lexists(destination):
        if not destination.is_dir():
            raise EvalFixtureError("TARGET_NOT_EMPTY", "materialization target must be empty")
        try:
            if next(destination.iterdir(), None) is not None:
                raise EvalFixtureError("TARGET_NOT_EMPTY", "materialization target must be empty")
        except OSError:
            raise EvalFixtureError(
                "TARGET_UNSAFE", "materialization target could not be inspected safely"
            ) from None
        return destination.resolve(strict=True)
    try:
        destination.mkdir()
        return destination.resolve(strict=True)
    except OSError:
        raise EvalFixtureError(
            "TARGET_UNSAFE", "materialization target could not be created safely"
        ) from None
