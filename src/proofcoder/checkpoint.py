"""Workspace checkpoints and rollback for one agent run.

A checkpoint is a content-addressed shadow copy of the workspace taken before the
first model call, so a run can be undone afterwards regardless of which process
performed a write. Only the standard library is used: no external version control
program, filesystem snapshot, or operating-system capability is required.

Constraints are specification section 10.5; the alternatives that were rejected are
recorded in ADR-0004. Two boundaries matter when reading this module:

* Sensitive paths never contribute content or a content digest to a checkpoint, and
  rollback never writes, deletes, or recreates them. They are reported instead.
* A checkpoint covers the captured scope only. Paths outside the workspace, version
  control internals, ignored directories, oversized files, and files that could not
  be read stay out of it, and every one of them is reported rather than skipped
  silently.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Collection, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from proofcoder.events import EventType
from proofcoder.safety.paths import ensure_within_workspace, is_internal_runtime_path
from proofcoder.safety.secrets import is_sensitive_path
from proofcoder.safety.writes import (
    commit_replacement,
    discard_temporary_file,
    stage_temporary_file,
)
from proofcoder.tools.files import DEFAULT_IGNORED_DIRECTORIES, MAX_FILE_SIZE_BYTES
from proofcoder.trace import TracePathError, read_trace, validate_run_id

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_RELATIVE_ROOT = Path(".proofcoder/checkpoints")
MANIFEST_FILENAME = "manifest.json"
BLOBS_DIRECTORY = "blobs"
DIRECTORY_MODE = 0o700

MAX_CHECKPOINT_FILE_BYTES = MAX_FILE_SIZE_BYTES
MAX_CHECKPOINT_ENTRIES = 20_000
MAX_CHECKPOINT_BYTES = 256 * 1024 * 1024
DEFAULT_RETAINED_CHECKPOINTS = 3
DEFAULT_RETAINED_BYTES = 512 * 1024 * 1024

_WRITING_TOOL_NAMES = frozenset({"create_file", "replace_in_file"})


class CheckpointError(Exception):
    """A stable checkpoint failure that is safe to display."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EntryState(StrEnum):
    """How much of one baseline file the checkpoint actually holds."""

    CAPTURED = "captured"
    OVERSIZE = "oversize"
    SENSITIVE = "sensitive"
    UNREADABLE = "unreadable"


class RollbackAction(StrEnum):
    """One planned filesystem action that undoes part of a run."""

    RESTORE = "restore"
    RECREATE = "recreate"
    DELETE = "delete"
    CREATE_DIRECTORY = "create_directory"
    REMOVE_DIRECTORY = "remove_directory"


class ChangeSource(StrEnum):
    """Where a covered change came from, as far as the trace can prove."""

    TOOL = "tool"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class CheckpointLimits:
    """Bounds on one capture and on how many checkpoints are retained."""

    max_file_bytes: int = MAX_CHECKPOINT_FILE_BYTES
    max_entries: int = MAX_CHECKPOINT_ENTRIES
    max_total_bytes: int = MAX_CHECKPOINT_BYTES
    retained_checkpoints: int = DEFAULT_RETAINED_CHECKPOINTS
    retained_bytes: int = DEFAULT_RETAINED_BYTES


DEFAULT_CHECKPOINT_LIMITS = CheckpointLimits()


@dataclass(frozen=True, slots=True)
class ScanSkips:
    """Paths the capture scope deliberately excluded, by category."""

    ignored_directories: int = 0
    symlinks: int = 0
    special_files: int = 0

    def to_dict(self) -> dict[str, int]:
        """Return the non-zero categories as a deterministic mapping."""

        counts = {
            "ignored_directories": self.ignored_directories,
            "symlinks": self.symlinks,
            "special_files": self.special_files,
        }
        return {name: value for name, value in counts.items() if value}


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    """One baseline file, with content only when the checkpoint may hold it."""

    path: str
    size: int
    mode: int
    modified_ns: int
    state: EntryState
    digest: str | None = None

    @property
    def restorable(self) -> bool:
        """Return whether rollback can put this file back byte for byte."""

        return self.state is EntryState.CAPTURED and self.digest is not None

    def to_dict(self) -> dict[str, object]:
        """Return the deterministic manifest form of this entry."""

        return {
            "digest": self.digest,
            "mode": self.mode,
            "modified_ns": self.modified_ns,
            "path": self.path,
            "size": self.size,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CheckpointEntry:
        """Rebuild one entry from manifest data, rejecting unusable records."""

        path = _manifest_path(value.get("path"))
        digest = value.get("digest")
        state = value.get("state")
        if digest is not None and not _is_digest(digest):
            raise CheckpointError("CHECKPOINT_CORRUPT", "manifest entry has an invalid digest")
        if not isinstance(state, str) or state not in set(EntryState):
            raise CheckpointError("CHECKPOINT_CORRUPT", "manifest entry has an unknown state")
        return cls(
            path=path,
            size=_manifest_int(value.get("size")),
            mode=_manifest_int(value.get("mode")),
            modified_ns=_manifest_int(value.get("modified_ns")),
            state=EntryState(state),
            digest=digest if isinstance(digest, str) else None,
        )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """The complete baseline recorded for one run."""

    run_id: str
    created_at: str
    entries: tuple[CheckpointEntry, ...]
    directories: tuple[str, ...]
    captured_bytes: int
    blob_count: int
    skips: ScanSkips

    def entry_map(self) -> dict[str, CheckpointEntry]:
        """Return entries keyed by workspace-relative path."""

        return {entry.path: entry for entry in self.entries}

    def count(self, state: EntryState) -> int:
        """Return how many baseline entries are in one state."""

        return sum(1 for entry in self.entries if entry.state is state)


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Checkpoints removed to stay inside the retention bounds."""

    removed_run_ids: tuple[str, ...] = ()
    freed_bytes: int = 0

    @property
    def removed_count(self) -> int:
        """Return how many checkpoints were removed."""

        return len(self.removed_run_ids)


@dataclass(frozen=True, slots=True)
class CheckpointCapture:
    """What one capture recorded, for the trace and the final report."""

    run_id: str
    created_at: str
    entry_count: int
    captured_count: int
    captured_bytes: int
    blob_count: int
    directory_count: int
    oversize_count: int
    sensitive_count: int
    unreadable_count: int
    skips: ScanSkips
    pruned: PruneResult = field(default_factory=PruneResult)


@dataclass(frozen=True, slots=True)
class CheckpointSummary:
    """Compact facts about one stored checkpoint."""

    run_id: str
    created_at: str
    entry_count: int
    stored_bytes: int
    readable: bool


@dataclass(frozen=True, slots=True)
class RollbackItem:
    """One path rollback will act on, with the source the trace proves."""

    path: str
    action: RollbackAction
    source: ChangeSource = ChangeSource.OTHER


@dataclass(frozen=True, slots=True)
class RollbackSkip:
    """One covered-scope gap reported instead of being acted on."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class RollbackFailure:
    """One planned action that could not be completed."""

    path: str
    action: RollbackAction
    code: str


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """Everything a rollback would change, shown before anything is written."""

    run_id: str
    items: tuple[RollbackItem, ...] = ()
    skipped: tuple[RollbackSkip, ...] = ()

    @property
    def empty(self) -> bool:
        """Return whether the workspace already matches the baseline."""

        return not self.items

    def paths_for(self, action: RollbackAction) -> tuple[str, ...]:
        """Return the planned paths for one action in plan order."""

        return tuple(item.path for item in self.items if item.action is action)


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """What a rollback actually did, including every part it could not do."""

    run_id: str
    restored: tuple[str, ...] = ()
    recreated: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    directories_created: tuple[str, ...] = ()
    directories_removed: tuple[str, ...] = ()
    skipped: tuple[RollbackSkip, ...] = ()
    failures: tuple[RollbackFailure, ...] = ()

    @property
    def complete(self) -> bool:
        """Return whether every planned action succeeded."""

        return not self.failures


def create_checkpoint(
    workspace: Path,
    run_id: str,
    *,
    limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CheckpointCapture:
    """Capture the workspace baseline for one run before any tool runs.

    Individual files that cannot be read are recorded as unreadable entries and
    reported, because one unreadable file is a coverage gap rather than a reason to
    refuse the run. A failure to create the checkpoint itself raises instead, so a
    run never starts believing it is protected when it is not.
    """

    workspace_root = _workspace_root(workspace)
    validated = _validate_run_id(run_id)
    pruned = prune_checkpoints(
        workspace_root,
        limits=limits,
        protected_run_id=validated,
        reserve=1,
    )
    directory = _create_checkpoint_directory(workspace_root, validated)
    created_at = _rfc3339(clock())
    try:
        files, directories, skips = _scan_workspace(workspace_root)
        entries, captured_bytes, blob_count = _capture_entries(
            workspace_root,
            directory,
            files,
            limits=limits,
        )
        checkpoint = Checkpoint(
            run_id=validated,
            created_at=created_at,
            entries=tuple(entries),
            directories=tuple(directories),
            captured_bytes=captured_bytes,
            blob_count=blob_count,
            skips=skips,
        )
        _write_manifest(directory, checkpoint)
    except BaseException:
        _remove_tree(directory)
        raise

    return CheckpointCapture(
        run_id=validated,
        created_at=created_at,
        entry_count=len(checkpoint.entries),
        captured_count=checkpoint.count(EntryState.CAPTURED),
        captured_bytes=captured_bytes,
        blob_count=blob_count,
        directory_count=len(checkpoint.directories),
        oversize_count=checkpoint.count(EntryState.OVERSIZE),
        sensitive_count=checkpoint.count(EntryState.SENSITIVE),
        unreadable_count=checkpoint.count(EntryState.UNREADABLE),
        skips=skips,
        pruned=pruned,
    )


def load_checkpoint(workspace: Path, run_id: str) -> Checkpoint:
    """Read one stored checkpoint manifest without touching workspace files."""

    workspace_root = _workspace_root(workspace)
    directory = _checkpoint_directory(workspace_root, _validate_run_id(run_id))
    manifest_path = directory / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise CheckpointError(
            "CHECKPOINT_NOT_FOUND",
            "no checkpoint exists for the requested run_id",
        )
    try:
        raw = manifest_path.read_bytes()
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_READ_ERROR",
            "checkpoint manifest could not be read safely",
        ) from None
    return _decode_manifest(raw, run_id=_validate_run_id(run_id))


def plan_rollback(
    workspace: Path,
    run_id: str,
    *,
    tool_written_paths: Collection[str] = (),
) -> RollbackPlan:
    """Compare the workspace with one baseline and return every planned action.

    Nothing is written. The plan exists so a user can see what a rollback would do
    before confirming it, which is what specification section 10.5.3 requires.
    """

    workspace_root = _workspace_root(workspace)
    checkpoint = load_checkpoint(workspace_root, run_id)
    files, directories, _ = _scan_workspace(workspace_root)
    written = {str(path) for path in tool_written_paths}

    baseline = checkpoint.entry_map()
    current = {scanned.path: scanned for scanned in files}
    items: list[RollbackItem] = []
    skipped: list[RollbackSkip] = []

    for path in sorted(baseline):
        entry = baseline[path]
        scanned = current.get(path)
        source = ChangeSource.TOOL if path in written else ChangeSource.OTHER
        if entry.state is EntryState.SENSITIVE:
            reason = _sensitive_change_reason(entry, scanned)
            if reason is not None:
                skipped.append(RollbackSkip(path, reason))
            continue
        if not entry.restorable:
            if _metadata_changed(entry, scanned):
                skipped.append(RollbackSkip(path, f"{entry.state.value}_changed"))
            continue
        if scanned is None:
            items.append(RollbackItem(path, RollbackAction.RECREATE, source))
        elif _content_differs(entry, scanned):
            items.append(RollbackItem(path, RollbackAction.RESTORE, source))

    for path in sorted(current):
        if path in baseline:
            continue
        if is_sensitive_path(path):
            skipped.append(RollbackSkip(path, "sensitive_created"))
            continue
        source = ChangeSource.TOOL if path in written else ChangeSource.OTHER
        items.append(RollbackItem(path, RollbackAction.DELETE, source))

    baseline_directories = set(checkpoint.directories)
    for path in sorted(baseline_directories - set(directories)):
        items.append(RollbackItem(path, RollbackAction.CREATE_DIRECTORY))
    for path in sorted(set(directories) - baseline_directories, reverse=True):
        items.append(RollbackItem(path, RollbackAction.REMOVE_DIRECTORY))

    return RollbackPlan(
        run_id=checkpoint.run_id,
        items=tuple(items),
        skipped=tuple(sorted(skipped, key=lambda skip: (skip.path, skip.reason))),
    )


def apply_rollback(workspace: Path, plan: RollbackPlan) -> RollbackResult:
    """Execute one plan and report every action that did not complete.

    Actions are independent: one failure never stops the rest, because leaving the
    workspace half restored without saying so is exactly what section 10.5.3 forbids.
    """

    workspace_root = _workspace_root(workspace)
    checkpoint = load_checkpoint(workspace_root, plan.run_id)
    directory = _checkpoint_directory(workspace_root, checkpoint.run_id)
    baseline = checkpoint.entry_map()

    restored: list[str] = []
    recreated: list[str] = []
    deleted: list[str] = []
    directories_created: list[str] = []
    directories_removed: list[str] = []
    failures: list[RollbackFailure] = []
    completed = {
        RollbackAction.RESTORE: restored,
        RollbackAction.RECREATE: recreated,
        RollbackAction.DELETE: deleted,
        RollbackAction.CREATE_DIRECTORY: directories_created,
        RollbackAction.REMOVE_DIRECTORY: directories_removed,
    }

    for item in _ordered_actions(plan):
        try:
            target = _safe_target(workspace_root, item.path)
        except CheckpointError as error:
            failures.append(RollbackFailure(item.path, item.action, error.code))
            continue
        try:
            _apply_action(directory, target, item, baseline.get(item.path))
        except CheckpointError as error:
            failures.append(RollbackFailure(item.path, item.action, error.code))
            continue
        completed[item.action].append(item.path)

    return RollbackResult(
        run_id=checkpoint.run_id,
        restored=tuple(restored),
        recreated=tuple(recreated),
        deleted=tuple(deleted),
        directories_created=tuple(directories_created),
        directories_removed=tuple(directories_removed),
        skipped=plan.skipped,
        failures=tuple(failures),
    )


def list_checkpoints(workspace: Path) -> tuple[CheckpointSummary, ...]:
    """Return every stored checkpoint, oldest first, without failing on damage."""

    workspace_root = _workspace_root(workspace)
    summaries = [
        CheckpointSummary(
            run_id=run_id,
            created_at="" if checkpoint is None else checkpoint.created_at,
            entry_count=0 if checkpoint is None else len(checkpoint.entries),
            stored_bytes=_directory_bytes(directory),
            readable=checkpoint is not None,
        )
        for run_id, directory, checkpoint in _stored_checkpoints(workspace_root)
    ]
    return tuple(sorted(summaries, key=lambda summary: (summary.created_at, summary.run_id)))


def prune_checkpoints(
    workspace: Path,
    *,
    limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
    protected_run_id: str | None = None,
    reserve: int = 0,
) -> PruneResult:
    """Delete the oldest checkpoints until the retention bounds hold again.

    Pruning runs synchronously before the next capture. The checkpoint of a run that
    is still in progress is protected, and run traces are never touched: they are the
    audit record and outlive the content they describe.

    ``reserve`` is the number of retention slots to leave free for a checkpoint that
    is about to be written, so "keep the most recent N runs" counts the incoming run
    rather than ending up with N + 1 stored checkpoints.
    """

    workspace_root = _workspace_root(workspace)
    summaries = [
        summary
        for summary in list_checkpoints(workspace_root)
        if summary.run_id != protected_run_id
    ]
    keep = max(0, limits.retained_checkpoints - max(0, reserve))
    total_bytes = sum(summary.stored_bytes for summary in summaries)

    removed: list[str] = []
    freed = 0
    for summary in summaries:
        within_count = len(summaries) - len(removed) <= keep
        if within_count and total_bytes - freed <= limits.retained_bytes:
            break
        freed += delete_checkpoint(workspace_root, summary.run_id)
        removed.append(summary.run_id)
    return PruneResult(removed_run_ids=tuple(removed), freed_bytes=freed)


def delete_checkpoint(workspace: Path, run_id: str) -> int:
    """Remove one checkpoint and return the bytes it occupied."""

    workspace_root = _workspace_root(workspace)
    directory = _checkpoint_directory(workspace_root, _validate_run_id(run_id))
    if not directory.is_dir():
        return 0
    freed = _directory_bytes(directory)
    _remove_tree(directory)
    return freed


def tool_written_paths(workspace: Path, run_id: str) -> tuple[str, ...]:
    """Return the paths this run's own file tools reported writing.

    Rollback uses these to label a change as tool-written rather than to decide what
    to restore; coverage never depends on knowing who performed a write.
    """

    try:
        trace = read_trace(workspace, run_id)
    except TracePathError:
        return ()
    paths: list[str] = []
    for event in trace.events:
        if event.event_type is not EventType.TOOL_RESULT:
            continue
        payload = event.payload
        path = payload.get("path")
        if (
            payload.get("tool_name") in _WRITING_TOOL_NAMES
            and payload.get("success") is True
            and isinstance(path, str)
            and path not in paths
        ):
            paths.append(path)
    return tuple(paths)


def checkpoint_event_payload(capture: CheckpointCapture | None) -> dict[str, object]:
    """Return the trace payload describing one capture, or its absence."""

    if capture is None:
        return {"captured": False, "reason": "disabled"}
    payload: dict[str, object] = {
        "blob_count": capture.blob_count,
        "captured": True,
        "captured_bytes": capture.captured_bytes,
        "captured_count": capture.captured_count,
        "directory_count": capture.directory_count,
        "entry_count": capture.entry_count,
    }
    uncovered = {
        "oversize": capture.oversize_count,
        "sensitive": capture.sensitive_count,
        "unreadable": capture.unreadable_count,
        **capture.skips.to_dict(),
    }
    uncovered = {name: value for name, value in uncovered.items() if value}
    if uncovered:
        payload["uncovered"] = uncovered
    if capture.pruned.removed_count:
        payload["pruned_checkpoints"] = capture.pruned.removed_count
        payload["pruned_bytes"] = capture.pruned.freed_bytes
    return payload


def rollback_event_payload(result: RollbackResult) -> dict[str, object]:
    """Return the trace payload describing one completed rollback."""

    payload: dict[str, object] = {
        "complete": result.complete,
        "deleted_count": len(result.deleted),
        "failed_count": len(result.failures),
        "recreated_count": len(result.recreated),
        "restored_count": len(result.restored),
        "skipped_count": len(result.skipped),
        "target_run_id": result.run_id,
    }
    if result.directories_created or result.directories_removed:
        payload["directories_created_count"] = len(result.directories_created)
        payload["directories_removed_count"] = len(result.directories_removed)
    if result.failures:
        payload["failures"] = [
            {"action": failure.action.value, "code": failure.code, "path": failure.path}
            for failure in result.failures
        ]
    if result.skipped:
        payload["skipped"] = [{"path": skip.path, "reason": skip.reason} for skip in result.skipped]
    return payload


@dataclass(frozen=True, slots=True)
class _ScannedFile:
    """One in-scope workspace file found by a scan."""

    path: str
    absolute: Path
    size: int
    mode: int
    modified_ns: int


def _scan_workspace(workspace_root: Path) -> tuple[list[_ScannedFile], list[str], ScanSkips]:
    """Walk the capture scope once, in deterministic order, following no symlink."""

    files: list[_ScannedFile] = []
    directories: list[str] = []
    ignored_directories = 0
    symlinks = 0
    special_files = 0

    pending = [(workspace_root, "")]
    while pending:
        current, prefix = pending.pop()
        try:
            with os.scandir(current) as scan:
                entries = sorted(scan, key=lambda item: item.name)
        except OSError:
            special_files += 1
            continue
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            try:
                if entry.is_symlink():
                    symlinks += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if is_internal_runtime_path(relative):
                        # ProofCoder's own run state is not part of the workspace and
                        # is not reported as a gap in what the checkpoint covers.
                        continue
                    if entry.name.casefold() in DEFAULT_IGNORED_DIRECTORIES:
                        ignored_directories += 1
                        continue
                    directories.append(relative)
                    pending.append((Path(entry.path), f"{relative}/"))
                    continue
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                special_files += 1
                continue
            if not stat.S_ISREG(metadata.st_mode):
                special_files += 1
                continue
            files.append(
                _ScannedFile(
                    path=relative,
                    absolute=Path(entry.path),
                    size=metadata.st_size,
                    mode=stat.S_IMODE(metadata.st_mode),
                    modified_ns=metadata.st_mtime_ns,
                )
            )

    files.sort(key=lambda item: item.path)
    directories.sort()
    return (
        files,
        directories,
        ScanSkips(
            ignored_directories=ignored_directories,
            symlinks=symlinks,
            special_files=special_files,
        ),
    )


def _capture_entries(
    workspace_root: Path,
    directory: Path,
    files: Iterable[_ScannedFile],
    *,
    limits: CheckpointLimits,
) -> tuple[list[CheckpointEntry], int, int]:
    """Record one entry per in-scope file, copying content only where permitted."""

    entries: list[CheckpointEntry] = []
    stored_digests: set[str] = set()
    captured_bytes = 0
    for scanned in files:
        if len(entries) >= limits.max_entries:
            raise CheckpointError(
                "CHECKPOINT_LIMIT_EXCEEDED",
                f"workspace exceeds the {limits.max_entries}-file checkpoint limit",
            )
        state = _entry_state(scanned, limits=limits)
        if state is not EntryState.CAPTURED:
            entries.append(_metadata_entry(scanned, state))
            continue
        try:
            content = scanned.absolute.read_bytes()
        except OSError:
            entries.append(_metadata_entry(scanned, EntryState.UNREADABLE))
            continue
        if len(content) > limits.max_file_bytes:
            entries.append(_metadata_entry(scanned, EntryState.OVERSIZE))
            continue
        captured_bytes += len(content)
        if captured_bytes > limits.max_total_bytes:
            raise CheckpointError(
                "CHECKPOINT_LIMIT_EXCEEDED",
                f"workspace exceeds the {limits.max_total_bytes}-byte checkpoint limit",
            )
        digest = hashlib.sha256(content).hexdigest()
        if digest not in stored_digests:
            _write_blob(workspace_root, directory, digest, content)
            stored_digests.add(digest)
        entries.append(
            CheckpointEntry(
                path=scanned.path,
                size=len(content),
                mode=scanned.mode,
                modified_ns=scanned.modified_ns,
                state=EntryState.CAPTURED,
                digest=digest,
            )
        )
    return entries, captured_bytes, len(stored_digests)


def _entry_state(scanned: _ScannedFile, *, limits: CheckpointLimits) -> EntryState:
    if is_sensitive_path(scanned.path):
        return EntryState.SENSITIVE
    if scanned.size > limits.max_file_bytes:
        return EntryState.OVERSIZE
    return EntryState.CAPTURED


def _metadata_entry(scanned: _ScannedFile, state: EntryState) -> CheckpointEntry:
    """Record one uncovered file by metadata alone, never by content or digest."""

    return CheckpointEntry(
        path=scanned.path,
        size=scanned.size,
        mode=scanned.mode,
        modified_ns=scanned.modified_ns,
        state=state,
    )


def _sensitive_change_reason(
    entry: CheckpointEntry,
    scanned: _ScannedFile | None,
) -> str | None:
    if scanned is None:
        return "sensitive_removed"
    if entry.size != scanned.size or entry.modified_ns != scanned.modified_ns:
        return "sensitive_changed"
    return None


def _metadata_changed(entry: CheckpointEntry, scanned: _ScannedFile | None) -> bool:
    if scanned is None:
        return True
    return entry.size != scanned.size or entry.modified_ns != scanned.modified_ns


def _content_differs(entry: CheckpointEntry, scanned: _ScannedFile) -> bool:
    """Return whether a covered file now differs from its captured content.

    The scan never follows a symbolic link, so the path being read here was reached
    through real directories inside the workspace.
    """

    if entry.size != scanned.size:
        return True
    try:
        current = scanned.absolute.read_bytes()
    except OSError:
        return True
    return hashlib.sha256(current).hexdigest() != entry.digest


def _ordered_actions(plan: RollbackPlan) -> list[RollbackItem]:
    """Order actions so directories exist before writes and empty after deletes."""

    order = {
        RollbackAction.CREATE_DIRECTORY: 0,
        RollbackAction.RESTORE: 1,
        RollbackAction.RECREATE: 1,
        RollbackAction.DELETE: 2,
        RollbackAction.REMOVE_DIRECTORY: 3,
    }
    return sorted(
        plan.items,
        key=lambda item: (
            order[item.action],
            -len(PurePosixPath(item.path).parts)
            if item.action is RollbackAction.REMOVE_DIRECTORY
            else len(PurePosixPath(item.path).parts),
            item.path,
        ),
    )


def _apply_action(
    directory: Path,
    target: Path,
    item: RollbackItem,
    entry: CheckpointEntry | None,
) -> None:
    if item.action is RollbackAction.CREATE_DIRECTORY:
        _make_directory(target)
        return
    if item.action is RollbackAction.REMOVE_DIRECTORY:
        _remove_directory(target)
        return
    if item.action is RollbackAction.DELETE:
        _remove_file(target)
        return
    if entry is None or entry.digest is None:
        raise CheckpointError("CHECKPOINT_CORRUPT", "planned restore has no captured content")
    _restore_file(directory, target, entry)


def _restore_file(directory: Path, target: Path, entry: CheckpointEntry) -> None:
    """Write one baseline file back atomically, creating parents when needed."""

    content = _read_blob(directory, entry.digest or "")
    if hashlib.sha256(content).hexdigest() != entry.digest:
        raise CheckpointError("CHECKPOINT_CORRUPT", "stored content does not match its digest")
    _make_directory(target.parent)
    if target.is_symlink():
        _remove_file(target)
    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(target, content, mode=entry.mode)
        commit_replacement(temporary, target)
        temporary = None
    except OSError:
        raise CheckpointError("CHECKPOINT_WRITE_ERROR", "file could not be written back") from None
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)
    with suppress(OSError):
        os.chmod(target, entry.mode)


def _remove_file(target: Path) -> None:
    try:
        if target.is_symlink() or target.exists():
            target.unlink()
    except OSError:
        raise CheckpointError("CHECKPOINT_DELETE_ERROR", "path could not be removed") from None


def _make_directory(target: Path) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_WRITE_ERROR",
            "directory could not be created",
        ) from None


def _remove_directory(target: Path) -> None:
    try:
        if not target.is_dir():
            return
        target.rmdir()
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_DELETE_ERROR",
            "directory could not be removed because it is not empty",
        ) from None


def _write_blob(workspace_root: Path, directory: Path, digest: str, content: bytes) -> None:
    blob = _blob_path(directory, digest)
    if blob.exists():
        return
    _make_checkpoint_subdirectory(workspace_root, directory / BLOBS_DIRECTORY)
    _make_checkpoint_subdirectory(workspace_root, blob.parent)
    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(blob, content)
        commit_replacement(temporary, blob)
        temporary = None
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_WRITE_ERROR",
            "checkpoint content could not be stored safely",
        ) from None
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)


def _read_blob(directory: Path, digest: str) -> bytes:
    if not _is_digest(digest):
        raise CheckpointError("CHECKPOINT_CORRUPT", "manifest entry has an invalid digest")
    try:
        return _blob_path(directory, digest).read_bytes()
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_READ_ERROR",
            "stored content is missing or unreadable",
        ) from None


def _make_checkpoint_subdirectory(workspace_root: Path, candidate: Path) -> None:
    """Create one checkpoint-owned directory with the restrictive run-directory mode."""

    _ensure_checkpoint_directory(workspace_root, candidate)
    if candidate.is_dir():
        return
    try:
        candidate.mkdir(mode=DIRECTORY_MODE)
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_WRITE_ERROR",
            "checkpoint directory could not be created inside the workspace",
        ) from None


def _blob_path(directory: Path, digest: str) -> Path:
    return directory / BLOBS_DIRECTORY / digest[:2] / digest


def _write_manifest(directory: Path, checkpoint: Checkpoint) -> None:
    payload = {
        "blob_count": checkpoint.blob_count,
        "captured_bytes": checkpoint.captured_bytes,
        "created_at": checkpoint.created_at,
        "directories": list(checkpoint.directories),
        "entries": [entry.to_dict() for entry in checkpoint.entries],
        "run_id": checkpoint.run_id,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "skips": checkpoint.skips.to_dict(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    target = directory / MANIFEST_FILENAME
    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(target, encoded.encode("utf-8"))
        commit_replacement(temporary, target)
        temporary = None
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_WRITE_ERROR",
            "checkpoint manifest could not be written safely",
        ) from None
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)


def _decode_manifest(raw: bytes, *, run_id: str) -> Checkpoint:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CheckpointError(
            "CHECKPOINT_CORRUPT", "checkpoint manifest is not valid JSON"
        ) from None
    if not isinstance(decoded, dict):
        raise CheckpointError("CHECKPOINT_CORRUPT", "checkpoint manifest is not an object")
    if decoded.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointError(
            "CHECKPOINT_SCHEMA_MISMATCH",
            "checkpoint manifest uses an unsupported schema version",
        )
    if decoded.get("run_id") != run_id:
        raise CheckpointError("CHECKPOINT_CORRUPT", "checkpoint manifest names a different run")

    raw_entries = decoded.get("entries")
    raw_directories = decoded.get("directories")
    if not isinstance(raw_entries, list) or not isinstance(raw_directories, list):
        raise CheckpointError("CHECKPOINT_CORRUPT", "checkpoint manifest is missing its records")
    entries: list[CheckpointEntry] = []
    for item in raw_entries:
        if not isinstance(item, Mapping):
            raise CheckpointError(
                "CHECKPOINT_CORRUPT",
                "checkpoint manifest has a malformed record",
            )
        entries.append(CheckpointEntry.from_dict(item))
    skips = decoded.get("skips")
    created_at = decoded.get("created_at")
    return Checkpoint(
        run_id=run_id,
        created_at=created_at if isinstance(created_at, str) else "",
        entries=tuple(entries),
        directories=tuple(_manifest_path(item) for item in raw_directories),
        captured_bytes=_manifest_int(decoded.get("captured_bytes")),
        blob_count=_manifest_int(decoded.get("blob_count")),
        skips=_decode_skips(skips),
    )


def _decode_skips(value: object) -> ScanSkips:
    if not isinstance(value, Mapping):
        return ScanSkips()
    return ScanSkips(
        ignored_directories=_manifest_int(value.get("ignored_directories", 0)),
        symlinks=_manifest_int(value.get("symlinks", 0)),
        special_files=_manifest_int(value.get("special_files", 0)),
    )


def _manifest_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise CheckpointError("CHECKPOINT_CORRUPT", "checkpoint manifest has an invalid number")
    return value


def _manifest_path(value: object) -> str:
    """Accept only relative, traversal-free POSIX paths from stored manifests."""

    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise CheckpointError("CHECKPOINT_CORRUPT", "checkpoint manifest has an invalid path")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise CheckpointError("CHECKPOINT_CORRUPT", "checkpoint manifest has an unsafe path")
    return value


def _is_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_target(workspace_root: Path, relative: str) -> Path:
    """Resolve one planned path again at apply time and keep it in the workspace."""

    target = workspace_root / PurePosixPath(_manifest_path(relative))
    try:
        ensure_within_workspace(workspace_root, target.parent)
    except Exception:
        raise CheckpointError(
            "CHECKPOINT_PATH_UNSAFE",
            "planned path resolves outside the selected workspace",
        ) from None
    return target


def _checkpoint_directory(workspace_root: Path, run_id: str) -> Path:
    path = workspace_root / CHECKPOINT_RELATIVE_ROOT / run_id
    _ensure_checkpoint_directory(workspace_root, path)
    return path


def _ensure_checkpoint_directory(workspace_root: Path, candidate: Path) -> None:
    try:
        ensure_within_workspace(workspace_root, candidate)
    except Exception:
        raise CheckpointError(
            "CHECKPOINT_PATH_UNSAFE",
            "checkpoint path resolves outside the selected workspace",
        ) from None


def _create_checkpoint_directory(workspace_root: Path, run_id: str) -> Path:
    current = workspace_root
    for part in (*CHECKPOINT_RELATIVE_ROOT.parts, run_id):
        candidate = current / part
        _ensure_checkpoint_directory(workspace_root, candidate)
        if candidate.is_symlink():
            raise CheckpointError(
                "CHECKPOINT_PATH_UNSAFE",
                "checkpoint path component is a symbolic link",
            )
        if candidate.exists():
            if part == run_id:
                raise CheckpointError(
                    "CHECKPOINT_EXISTS",
                    "a checkpoint already exists for this run",
                )
            if not candidate.is_dir():
                raise CheckpointError(
                    "CHECKPOINT_PATH_UNSAFE",
                    "checkpoint path component is not a directory",
                )
        else:
            try:
                candidate.mkdir(mode=DIRECTORY_MODE)
            except OSError:
                raise CheckpointError(
                    "CHECKPOINT_WRITE_ERROR",
                    "checkpoint directory could not be created inside the workspace",
                ) from None
        current = candidate
    return current


def _stored_checkpoints(
    workspace_root: Path,
) -> list[tuple[str, Path, Checkpoint | None]]:
    root = workspace_root / CHECKPOINT_RELATIVE_ROOT
    _ensure_checkpoint_directory(workspace_root, root)
    if not root.is_dir():
        return []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        raise CheckpointError(
            "CHECKPOINT_READ_ERROR",
            "checkpoint root could not be listed safely",
        ) from None

    stored: list[tuple[str, Path, Checkpoint | None]] = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            run_id = validate_run_id(entry.name)
        except TracePathError:
            continue
        try:
            checkpoint: Checkpoint | None = load_checkpoint(workspace_root, run_id)
        except CheckpointError:
            checkpoint = None
        stored.append((run_id, entry, checkpoint))
    return stored


def _directory_bytes(directory: Path) -> int:
    total = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as scan:
                entries = list(scan)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def _remove_tree(directory: Path) -> None:
    """Remove one checkpoint directory without following symbolic links."""

    if not directory.is_dir() or directory.is_symlink():
        return
    pending = [directory]
    visited: list[Path] = []
    while pending:
        current = pending.pop()
        visited.append(current)
        try:
            with os.scandir(current) as scan:
                entries = list(scan)
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                is_directory = entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
            except OSError:
                is_directory = False
            if is_directory:
                pending.append(path)
            else:
                with suppress(OSError):
                    path.unlink()
    for current in reversed(visited):
        with suppress(OSError):
            current.rmdir()


def _workspace_root(workspace: Path) -> Path:
    try:
        root = workspace.resolve(strict=True)
    except OSError:
        raise CheckpointError(
            "INVALID_WORKSPACE", "workspace must be an existing directory"
        ) from None
    if not root.is_dir():
        raise CheckpointError("INVALID_WORKSPACE", "workspace must be an existing directory")
    return root


def _validate_run_id(run_id: str) -> str:
    try:
        return validate_run_id(run_id)
    except TracePathError as error:
        raise CheckpointError("INVALID_RUN_ID", str(error)) from None


def _rfc3339(value: datetime) -> str:
    normalized = (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
