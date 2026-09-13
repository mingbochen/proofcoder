"""Capture scope, rollback coverage, and retention tests for Stage F checkpoints."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from proofcoder.checkpoint import (
    BLOBS_DIRECTORY,
    CHECKPOINT_RELATIVE_ROOT,
    MANIFEST_FILENAME,
    ChangeSource,
    Checkpoint,
    CheckpointError,
    CheckpointLimits,
    EntryState,
    RollbackAction,
    apply_rollback,
    checkpoint_event_payload,
    create_checkpoint,
    delete_checkpoint,
    list_checkpoints,
    load_checkpoint,
    plan_rollback,
    prune_checkpoints,
    rollback_event_payload,
    tool_written_paths,
)

# Written into a workspace .env so the tests can prove no checkpoint artifact ever
# contains it. It is a marker, not a credential.
SENSITIVE_SENTINEL = "never-store-this-value"

RUN_A = "a" * 32
RUN_B = "b" * 32
RUN_C = "c" * 32
SMALL_LIMITS = CheckpointLimits(max_file_bytes=1024)


def _workspace(root: Path) -> Path:
    """Build one workspace covering every capture-scope category."""

    (root / "pkg").mkdir()
    (root / "pkg" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "removed.txt").write_text("removed\n", encoding="utf-8")
    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}\n", encoding="utf-8")
    (root / "large.bin").write_bytes(b"x" * 2048)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "vendor.js").write_text("vendor\n", encoding="utf-8")
    return root


def _checkpoint_bytes(root: Path) -> bytes:
    """Return every byte stored under the checkpoint root."""

    stored = b""
    for path in sorted((root / CHECKPOINT_RELATIVE_ROOT).rglob("*")):
        if path.is_file():
            stored += path.read_bytes()
    return stored


def _manifest(root: Path, run_id: str) -> dict[str, object]:
    path = root / CHECKPOINT_RELATIVE_ROOT / run_id / MANIFEST_FILENAME
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(root: Path, run_id: str, payload: object) -> None:
    path = root / CHECKPOINT_RELATIVE_ROOT / run_id / MANIFEST_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_capture_scope_covers_files_and_reports_every_gap(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    if sys.platform != "win32":
        (root / "link.txt").symlink_to(root / "keep.txt")

    capture = create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    checkpoint = load_checkpoint(root, RUN_A)
    states = {entry.path: entry.state for entry in checkpoint.entries}
    assert states["pkg/app.py"] is EntryState.CAPTURED
    assert states["keep.txt"] is EntryState.CAPTURED
    assert states["large.bin"] is EntryState.OVERSIZE
    assert states[".env"] is EntryState.SENSITIVE
    # Ignored directories never produce entries at all.
    assert not any(path.startswith("node_modules/") for path in states)
    assert "node_modules" not in checkpoint.directories
    assert checkpoint.directories == ("pkg",)
    assert capture.oversize_count == 1
    assert capture.sensitive_count == 1
    assert capture.skips.ignored_directories == 1
    if sys.platform != "win32":
        assert capture.skips.symlinks == 1
        assert "link.txt" not in states


def test_sensitive_paths_contribute_no_content_and_no_digest(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    entry = next(item for item in load_checkpoint(root, RUN_A).entries if item.path == ".env")
    assert entry.state is EntryState.SENSITIVE
    assert entry.digest is None
    assert not entry.restorable
    assert entry.size > 0
    # Neither the value nor any digest derived from the file may be stored.
    stored = _checkpoint_bytes(root)
    assert SENSITIVE_SENTINEL.encode("utf-8") not in stored
    env_digest = __import__("hashlib").sha256((root / ".env").read_bytes()).hexdigest()
    assert env_digest.encode("ascii") not in stored


def test_oversize_files_are_detectable_but_not_restorable(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    (root / "large.bin").write_bytes(b"y" * 2048)
    plan = plan_rollback(root, RUN_A)

    assert "large.bin" not in [item.path for item in plan.items]
    assert ("large.bin", "oversize_changed") in [(skip.path, skip.reason) for skip in plan.skipped]


def test_identical_content_is_stored_once(tmp_path: Path) -> None:
    (tmp_path / "first.txt").write_text("same\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("same\n", encoding="utf-8")

    capture = create_checkpoint(tmp_path, RUN_A)

    assert capture.captured_count == 2
    assert capture.blob_count == 1
    blobs = list((tmp_path / CHECKPOINT_RELATIVE_ROOT / RUN_A / BLOBS_DIRECTORY).rglob("*"))
    assert len([path for path in blobs if path.is_file()]) == 1


def test_rollback_restores_every_change_source(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    # One tool write, one workspace-script write, one creation, one deletion.
    (root / "pkg" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (root / "keep.txt").write_text("script rewrote this\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "artifact.o").write_text("object\n", encoding="utf-8")
    (root / "removed.txt").unlink()

    plan = plan_rollback(root, RUN_A, tool_written_paths=["pkg/app.py"])
    sources = {item.path: item.source for item in plan.items}
    assert sources["pkg/app.py"] is ChangeSource.TOOL
    assert sources["keep.txt"] is ChangeSource.OTHER
    assert sources["build/artifact.o"] is ChangeSource.OTHER

    result = apply_rollback(root, plan)

    assert result.complete
    assert (root / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (root / "removed.txt").read_text(encoding="utf-8") == "removed\n"
    assert not (root / "build" / "artifact.o").exists()
    assert not (root / "build").exists()
    assert set(result.restored) == {"pkg/app.py", "keep.txt"}
    assert result.recreated == ("removed.txt",)
    assert result.deleted == ("build/artifact.o",)
    assert result.directories_removed == ("build",)


def test_rollback_never_touches_sensitive_paths(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}-rotated\n", encoding="utf-8")
    (root / "secrets.json").write_text("{}\n", encoding="utf-8")

    plan = plan_rollback(root, RUN_A)
    reasons = {skip.path: skip.reason for skip in plan.skipped}

    assert ".env" not in [item.path for item in plan.items]
    assert "secrets.json" not in [item.path for item in plan.items]
    assert reasons[".env"] == "sensitive_changed"
    assert reasons["secrets.json"] == "sensitive_created"

    apply_rollback(root, plan)

    assert (root / ".env").read_text(encoding="utf-8").endswith("-rotated\n")
    assert (root / "secrets.json").is_file()


def test_removed_sensitive_file_is_reported_and_not_recreated(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)
    (root / ".env").unlink()

    plan = plan_rollback(root, RUN_A)
    apply_rollback(root, plan)

    assert (".env", "sensitive_removed") in [(skip.path, skip.reason) for skip in plan.skipped]
    assert not (root / ".env").exists()


def test_rollback_is_idempotent(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)
    (root / "keep.txt").write_text("changed\n", encoding="utf-8")

    first = apply_rollback(root, plan_rollback(root, RUN_A))
    second_plan = plan_rollback(root, RUN_A)
    second = apply_rollback(root, second_plan)

    assert first.restored == ("keep.txt",)
    assert second_plan.empty
    assert second.restored == ()
    assert second.complete
    assert (root / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_partial_failure_reports_the_paths_it_could_not_restore(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)
    (root / "keep.txt").write_text("changed\n", encoding="utf-8")
    (root / "pkg" / "app.py").write_text("value = 9\n", encoding="utf-8")

    # Losing stored content must be reported, not silently skipped.
    entry = next(item for item in load_checkpoint(root, RUN_A).entries if item.path == "keep.txt")
    assert entry.digest is not None
    blob = (
        root / CHECKPOINT_RELATIVE_ROOT / RUN_A / BLOBS_DIRECTORY / entry.digest[:2] / entry.digest
    )
    blob.unlink()

    result = apply_rollback(root, plan_rollback(root, RUN_A))

    assert not result.complete
    assert [failure.path for failure in result.failures] == ["keep.txt"]
    assert result.failures[0].code == "CHECKPOINT_READ_ERROR"
    assert result.failures[0].action is RollbackAction.RESTORE
    # Every other planned action still ran.
    assert result.restored == ("pkg/app.py",)
    assert (root / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_rollback_recreates_missing_parent_directories(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    (root / "pkg" / "app.py").unlink()
    (root / "pkg").rmdir()

    result = apply_rollback(root, plan_rollback(root, RUN_A))

    assert result.complete
    assert (root / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert result.directories_created == ("pkg",)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_restored_files_keep_their_permission_bits(tmp_path: Path) -> None:
    script = tmp_path / "tool.sh"
    script.write_text("echo one\n", encoding="utf-8")
    script.chmod(0o750)
    create_checkpoint(tmp_path, RUN_A)

    script.write_text("echo two\n", encoding="utf-8")
    script.chmod(0o600)
    apply_rollback(tmp_path, plan_rollback(tmp_path, RUN_A))

    assert script.read_text(encoding="utf-8") == "echo one\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o750


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links")
def test_symlink_created_during_a_run_is_removed_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("host file\n", encoding="utf-8")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(root, RUN_A)

    (root / "escape.txt").symlink_to(outside)
    plan = plan_rollback(root, RUN_A)
    result = apply_rollback(root, plan)

    # The link is not in scope, so nothing about it is planned and the target survives.
    assert plan.empty
    assert result.complete
    assert (root / "escape.txt").is_symlink()
    assert outside.read_text(encoding="utf-8") == "host file\n"


def test_capture_refuses_a_workspace_over_the_entry_limit(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"file{index}.txt").write_text("data\n", encoding="utf-8")

    with pytest.raises(CheckpointError) as error:
        create_checkpoint(tmp_path, RUN_A, limits=CheckpointLimits(max_entries=2))

    assert error.value.code == "CHECKPOINT_LIMIT_EXCEEDED"
    # A refused capture leaves nothing behind to be mistaken for protection.
    assert not (tmp_path / CHECKPOINT_RELATIVE_ROOT / RUN_A).exists()


def test_capture_refuses_a_workspace_over_the_byte_limit(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("x" * 512, encoding="utf-8")

    with pytest.raises(CheckpointError) as error:
        create_checkpoint(tmp_path, RUN_A, limits=CheckpointLimits(max_total_bytes=16))

    assert error.value.code == "CHECKPOINT_LIMIT_EXCEEDED"
    assert not (tmp_path / CHECKPOINT_RELATIVE_ROOT / RUN_A).exists()


def test_capture_refuses_to_overwrite_an_existing_checkpoint(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)

    with pytest.raises(CheckpointError) as error:
        create_checkpoint(tmp_path, RUN_A)

    assert error.value.code == "CHECKPOINT_EXISTS"
    assert load_checkpoint(tmp_path, RUN_A).entries


def test_unreadable_files_are_recorded_as_gaps_rather_than_failing_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "readable.txt").write_text("fine\n", encoding="utf-8")
    (tmp_path / "blocked.txt").write_text("denied\n", encoding="utf-8")
    original = Path.read_bytes

    def read_bytes(self: Path) -> bytes:
        if self.name == "blocked.txt":
            raise PermissionError("denied")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    capture = create_checkpoint(tmp_path, RUN_A)

    assert capture.unreadable_count == 1
    assert capture.captured_count == 1
    entry = next(
        item for item in load_checkpoint(tmp_path, RUN_A).entries if item.path == "blocked.txt"
    )
    assert entry.state is EntryState.UNREADABLE
    assert entry.digest is None


def test_unreadable_entry_changes_are_reported_and_not_restored(tmp_path: Path) -> None:
    (tmp_path / "blocked.txt").write_text("denied\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    _write_manifest(
        tmp_path,
        RUN_A,
        {
            **_manifest(tmp_path, RUN_A),
            "entries": [
                {
                    "digest": None,
                    "mode": 420,
                    "modified_ns": 1,
                    "path": "blocked.txt",
                    "size": 7,
                    "state": "unreadable",
                }
            ],
        },
    )
    (tmp_path / "blocked.txt").write_text("changed by a script\n", encoding="utf-8")

    plan = plan_rollback(tmp_path, RUN_A)

    assert plan.empty
    assert [(skip.path, skip.reason) for skip in plan.skipped] == [
        ("blocked.txt", "unreadable_changed")
    ]


def test_retention_prunes_oldest_first_and_protects_the_running_checkpoint(
    tmp_path: Path,
) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    for index, run_id in enumerate((RUN_A, RUN_B, RUN_C)):
        create_checkpoint(
            tmp_path,
            run_id,
            limits=CheckpointLimits(retained_checkpoints=10),
            clock=lambda index=index: __import__("datetime").datetime(
                2026, 1, 1 + index, tzinfo=__import__("datetime").UTC
            ),
        )

    pruned = prune_checkpoints(
        tmp_path,
        limits=CheckpointLimits(retained_checkpoints=1),
        protected_run_id=RUN_C,
    )

    assert pruned.removed_run_ids == (RUN_A,)
    assert pruned.freed_bytes > 0
    assert {summary.run_id for summary in list_checkpoints(tmp_path)} == {RUN_B, RUN_C}


def test_retention_also_honors_the_total_byte_limit(tmp_path: Path) -> None:
    (tmp_path / "payload.txt").write_text("x" * 4096, encoding="utf-8")
    for index, run_id in enumerate((RUN_A, RUN_B)):
        create_checkpoint(
            tmp_path,
            run_id,
            limits=CheckpointLimits(retained_checkpoints=10),
            clock=lambda index=index: __import__("datetime").datetime(
                2026, 1, 1 + index, tzinfo=__import__("datetime").UTC
            ),
        )

    stored = {summary.run_id: summary.stored_bytes for summary in list_checkpoints(tmp_path)}
    # A cap that fits the newer checkpoint but not both must drop only the older one.
    pruned = prune_checkpoints(
        tmp_path,
        limits=CheckpointLimits(retained_checkpoints=10, retained_bytes=stored[RUN_B]),
    )

    assert pruned.removed_run_ids == (RUN_A,)
    assert [summary.run_id for summary in list_checkpoints(tmp_path)] == [RUN_B]


def test_capture_prunes_before_recording_the_new_baseline(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    limits = CheckpointLimits(retained_checkpoints=1)
    create_checkpoint(tmp_path, RUN_A, limits=limits)

    capture = create_checkpoint(tmp_path, RUN_B, limits=limits)

    assert capture.pruned.removed_run_ids == (RUN_A,)
    assert {summary.run_id for summary in list_checkpoints(tmp_path)} == {RUN_B}


def test_pruning_never_removes_run_traces(tmp_path: Path) -> None:
    from proofcoder.trace import TRACE_RELATIVE_ROOT, TraceRecorder

    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    TraceRecorder(tmp_path, RUN_A).close()
    create_checkpoint(tmp_path, RUN_A)

    prune_checkpoints(tmp_path, limits=CheckpointLimits(retained_checkpoints=0))

    assert list_checkpoints(tmp_path) == ()
    assert (tmp_path / TRACE_RELATIVE_ROOT / RUN_A).is_dir()


def test_damaged_manifests_are_listed_but_never_loaded(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    _write_manifest(tmp_path, RUN_A, {"schema_version": 1, "run_id": RUN_A})

    summaries = list_checkpoints(tmp_path)

    assert [summary.readable for summary in summaries] == [False]
    with pytest.raises(CheckpointError) as error:
        load_checkpoint(tmp_path, RUN_A)
    assert error.value.code == "CHECKPOINT_CORRUPT"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../escape.txt", "CHECKPOINT_CORRUPT"),
        ("/etc/passwd", "CHECKPOINT_CORRUPT"),
        ("nested/../../escape.txt", "CHECKPOINT_CORRUPT"),
    ],
)
def test_manifest_paths_that_leave_the_workspace_are_rejected(
    tmp_path: Path,
    path: str,
    code: str,
) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    manifest = _manifest(tmp_path, RUN_A)
    manifest["entries"] = [
        {**entry, "path": path}
        for entry in manifest["entries"]  # type: ignore[union-attr]
    ]
    _write_manifest(tmp_path, RUN_A, manifest)

    with pytest.raises(CheckpointError) as error:
        load_checkpoint(tmp_path, RUN_A)

    assert error.value.code == code


def test_manifest_schema_and_run_mismatch_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    original = _manifest(tmp_path, RUN_A)

    _write_manifest(tmp_path, RUN_A, {**original, "schema_version": 99})
    with pytest.raises(CheckpointError) as schema_error:
        load_checkpoint(tmp_path, RUN_A)

    _write_manifest(tmp_path, RUN_A, {**original, "run_id": RUN_B})
    with pytest.raises(CheckpointError) as run_error:
        load_checkpoint(tmp_path, RUN_A)

    assert schema_error.value.code == "CHECKPOINT_SCHEMA_MISMATCH"
    assert run_error.value.code == "CHECKPOINT_CORRUPT"


def test_missing_checkpoint_and_invalid_run_id_are_distinct_failures(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError) as missing:
        load_checkpoint(tmp_path, RUN_A)
    with pytest.raises(CheckpointError) as invalid:
        load_checkpoint(tmp_path, "../escape")

    assert missing.value.code == "CHECKPOINT_NOT_FOUND"
    assert invalid.value.code == "INVALID_RUN_ID"


def test_checkpoint_state_is_never_captured_by_the_next_checkpoint(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)

    capture = create_checkpoint(tmp_path, RUN_B, limits=CheckpointLimits(retained_checkpoints=10))

    paths = {entry.path for entry in load_checkpoint(tmp_path, RUN_B).entries}
    assert paths == {"keep.txt"}
    assert capture.entry_count == 1


def test_delete_checkpoint_reports_freed_bytes_and_is_safe_to_repeat(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)

    freed = delete_checkpoint(tmp_path, RUN_A)
    again = delete_checkpoint(tmp_path, RUN_A)

    assert freed > 0
    assert again == 0
    assert list_checkpoints(tmp_path) == ()


def test_tool_written_paths_come_from_the_recorded_trace(tmp_path: Path) -> None:
    from proofcoder.events import EventType, RunEvent
    from proofcoder.trace import TraceRecorder

    recorder = TraceRecorder(tmp_path, RUN_A)
    try:
        for sequence, payload in enumerate(
            [
                {"tool_name": "create_file", "success": True, "path": "created.txt"},
                {"tool_name": "replace_in_file", "success": True, "path": "edited.txt"},
                {"tool_name": "replace_in_file", "success": False, "path": "failed.txt"},
                {"tool_name": "read_file", "success": True, "path": "read.txt"},
            ],
            start=1,
        ):
            recorder.emit(
                RunEvent(
                    run_id=RUN_A,
                    sequence=sequence,
                    step=1,
                    timestamp="2026-01-01T00:00:00.000000Z",
                    event_type=EventType.TOOL_RESULT,
                    payload=payload,
                )
            )
        recorder.emit(
            RunEvent(
                run_id=RUN_A,
                sequence=5,
                step=1,
                timestamp="2026-01-01T00:00:00.000000Z",
                event_type=EventType.TERMINATION,
                payload={"termination_reason": "finish_task", "trace_complete": True},
            )
        )
    finally:
        recorder.close()

    assert tool_written_paths(tmp_path, RUN_A) == ("created.txt", "edited.txt")
    assert tool_written_paths(tmp_path, RUN_B) == ()


def test_event_payloads_summarize_coverage_and_outcome(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    capture = create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)
    (root / "keep.txt").write_text("changed\n", encoding="utf-8")
    result = apply_rollback(root, plan_rollback(root, RUN_A))

    captured = checkpoint_event_payload(capture)
    rolled_back = rollback_event_payload(result)

    assert captured["captured"] is True
    assert captured["entry_count"] == capture.entry_count
    assert captured["uncovered"] == {
        "ignored_directories": 1,
        "oversize": 1,
        "sensitive": 1,
    }
    assert checkpoint_event_payload(None) == {"captured": False, "reason": "disabled"}
    assert rolled_back["restored_count"] == 1
    assert rolled_back["complete"] is True
    assert rolled_back["target_run_id"] == RUN_A
    assert rolled_back["skipped_count"] == 0
    assert "skipped" not in rolled_back

    # A reported gap is named in the payload, not just counted.
    (root / ".env").write_text("TOKEN_NAME=rotated\n", encoding="utf-8")
    with_gap = rollback_event_payload(apply_rollback(root, plan_rollback(root, RUN_A)))
    assert with_gap["skipped"] == [{"path": ".env", "reason": "sensitive_changed"}]
    # Payloads carry paths and counts only, never file content.
    assert "content" not in json.dumps(captured)
    assert SENSITIVE_SENTINEL not in json.dumps(rolled_back)


def test_checkpoint_directory_is_not_world_readable(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)

    directory = tmp_path / CHECKPOINT_RELATIVE_ROOT / RUN_A
    if sys.platform != "win32":
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert directory.is_dir()


def test_workspace_must_exist(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError) as error:
        create_checkpoint(tmp_path / "missing", RUN_A)

    assert error.value.code == "INVALID_WORKSPACE"


def test_checkpoint_helpers_expose_stable_facts(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)
    checkpoint: Checkpoint = load_checkpoint(root, RUN_A)

    assert checkpoint.run_id == RUN_A
    assert checkpoint.count(EntryState.CAPTURED) == 3
    assert set(checkpoint.entry_map()) == {entry.path for entry in checkpoint.entries}
    assert os.sep not in "".join(entry.path for entry in checkpoint.entries) or os.sep == "/"


def test_directory_left_non_empty_by_ignored_content_is_reported(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)

    # A build directory whose only survivor is an ignored cache cannot be removed.
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("object\n", encoding="utf-8")
    (tmp_path / "build" / "__pycache__").mkdir()
    (tmp_path / "build" / "__pycache__" / "cached.pyc").write_bytes(b"\x00")

    result = apply_rollback(tmp_path, plan_rollback(tmp_path, RUN_A))

    assert result.deleted == ("build/out.o",)
    assert not result.complete
    assert [(failure.path, failure.code) for failure in result.failures] == [
        ("build", "CHECKPOINT_DELETE_ERROR")
    ]
    assert (tmp_path / "build" / "__pycache__" / "cached.pyc").exists()


def test_corrupted_stored_content_is_never_written_back(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    entry = next(item for item in load_checkpoint(tmp_path, RUN_A).entries)
    assert entry.digest is not None
    blob = (
        tmp_path
        / CHECKPOINT_RELATIVE_ROOT
        / RUN_A
        / BLOBS_DIRECTORY
        / entry.digest[:2]
        / entry.digest
    )
    blob.write_bytes(b"tampered content\n")
    (tmp_path / "keep.txt").write_text("changed\n", encoding="utf-8")

    result = apply_rollback(tmp_path, plan_rollback(tmp_path, RUN_A))

    assert [failure.code for failure in result.failures] == ["CHECKPOINT_CORRUPT"]
    # The workspace keeps its current content rather than receiving tampered bytes.
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "changed\n"


def test_write_failures_during_rollback_are_reported_per_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.checkpoint as checkpoint_module

    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    (tmp_path / "keep.txt").write_text("changed\n", encoding="utf-8")

    def failing_stage(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(checkpoint_module, "stage_temporary_file", failing_stage)
    result = apply_rollback(tmp_path, plan_rollback(tmp_path, RUN_A))

    assert [(failure.path, failure.code) for failure in result.failures] == [
        ("keep.txt", "CHECKPOINT_WRITE_ERROR")
    ]
    assert result.restored == ()


def test_delete_failures_during_rollback_are_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    (tmp_path / "extra.txt").write_text("extra\n", encoding="utf-8")
    original = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "extra.txt":
            raise OSError("busy")
        original(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    result = apply_rollback(tmp_path, plan_rollback(tmp_path, RUN_A))

    assert [(failure.path, failure.code) for failure in result.failures] == [
        ("extra.txt", "CHECKPOINT_DELETE_ERROR")
    ]
    assert (tmp_path / "extra.txt").exists()


def test_planned_paths_are_revalidated_before_anything_is_written(tmp_path: Path) -> None:
    from proofcoder.checkpoint import RollbackItem, RollbackPlan

    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    forged = RollbackPlan(
        run_id=RUN_A,
        items=(RollbackItem("../escape.txt", RollbackAction.DELETE),),
    )

    result = apply_rollback(tmp_path, forged)

    assert [(failure.path, failure.code) for failure in result.failures] == [
        ("../escape.txt", "CHECKPOINT_CORRUPT")
    ]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_checkpoint_root_entries_that_are_not_runs_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    root = tmp_path / CHECKPOINT_RELATIVE_ROOT
    (root / "not-a-run-id").mkdir()
    (root / "stray.txt").write_text("stray\n", encoding="utf-8")

    summaries = list_checkpoints(tmp_path)

    assert [summary.run_id for summary in summaries] == [RUN_A]


def test_listing_an_untouched_workspace_is_empty(tmp_path: Path) -> None:
    assert list_checkpoints(tmp_path) == ()
    assert prune_checkpoints(tmp_path).removed_run_ids == ()


def test_capture_rejects_a_blocked_checkpoint_root(tmp_path: Path) -> None:
    (tmp_path / ".proofcoder").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(CheckpointError) as error:
        create_checkpoint(tmp_path, RUN_A)

    assert error.value.code == "CHECKPOINT_PATH_UNSAFE"


def test_manifest_numbers_and_states_must_be_well_formed(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    create_checkpoint(tmp_path, RUN_A)
    original = _manifest(tmp_path, RUN_A)

    for mutation in (
        {"entries": [{**original["entries"][0], "size": -1}]},  # type: ignore[index]
        {"entries": [{**original["entries"][0], "state": "unknown"}]},  # type: ignore[index]
        {"entries": [{**original["entries"][0], "digest": "nothex"}]},  # type: ignore[index]
        {"entries": ["not an object"]},
        {"entries": "not a list"},
    ):
        _write_manifest(tmp_path, RUN_A, {**original, **mutation})
        with pytest.raises(CheckpointError) as error:
            load_checkpoint(tmp_path, RUN_A)
        assert error.value.code == "CHECKPOINT_CORRUPT"


def test_manifest_skips_survive_a_round_trip_and_tolerate_damage(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)

    assert load_checkpoint(root, RUN_A).skips.ignored_directories == 1

    _write_manifest(root, RUN_A, {**_manifest(root, RUN_A), "skips": "damaged"})
    assert load_checkpoint(root, RUN_A).skips.ignored_directories == 0


def test_capture_payload_reports_pruning(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    limits = CheckpointLimits(retained_checkpoints=1)
    create_checkpoint(tmp_path, RUN_A, limits=limits)
    capture = create_checkpoint(tmp_path, RUN_B, limits=limits)

    payload = checkpoint_event_payload(capture)

    assert payload["pruned_checkpoints"] == 1
    assert payload["pruned_bytes"] > 0


def test_terminal_rendering_states_what_is_not_covered(tmp_path: Path) -> None:
    from proofcoder.events import EventType, RunEvent, render_terminal_event

    root = _workspace(tmp_path)
    capture = create_checkpoint(root, RUN_A, limits=SMALL_LIMITS)
    (root / "keep.txt").write_text("changed\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN_NAME=rotated\n", encoding="utf-8")
    result = apply_rollback(root, plan_rollback(root, RUN_A))

    def render(event_type: EventType, payload: dict[str, object]) -> str:
        line = render_terminal_event(
            RunEvent(
                run_id=RUN_A,
                sequence=1,
                step=0,
                timestamp="2026-01-01T00:00:00.000000Z",
                event_type=event_type,
                payload=payload,
            )
        )
        assert line is not None
        return line

    captured = render(EventType.CHECKPOINT, checkpoint_event_payload(capture))
    disabled = render(EventType.CHECKPOINT, checkpoint_event_payload(None))
    rolled_back = render(EventType.ROLLBACK, rollback_event_payload(result))

    assert captured.startswith("CHECKPOINT: entries=5 captured=3")
    assert "uncovered=ignored_directories:1,oversize:1,sensitive:1" in captured
    assert disabled == "CHECKPOINT: none reason=disabled"
    assert rolled_back.startswith(f"ROLLBACK: run_id={RUN_A} restored=1")
    assert "skipped=1" in rolled_back
    assert "failed=0" in rolled_back


def test_incomplete_rollback_is_visible_in_its_rendering(tmp_path: Path) -> None:
    from proofcoder.checkpoint import RollbackFailure, RollbackResult
    from proofcoder.events import EventType, RunEvent, render_terminal_event

    payload = rollback_event_payload(
        RollbackResult(
            run_id=RUN_A,
            failures=(RollbackFailure("a.txt", RollbackAction.RESTORE, "CHECKPOINT_READ_ERROR"),),
        )
    )
    line = render_terminal_event(
        RunEvent(
            run_id=RUN_A,
            sequence=1,
            step=0,
            timestamp="2026-01-01T00:00:00.000000Z",
            event_type=EventType.ROLLBACK,
            payload=payload,
        )
    )

    assert payload["failures"] == [
        {"action": "restore", "code": "CHECKPOINT_READ_ERROR", "path": "a.txt"}
    ]
    assert line is not None
    assert "complete=false" in line
    assert "failed=1" in line


def test_tool_written_paths_tolerate_a_missing_or_damaged_trace(tmp_path: Path) -> None:
    from proofcoder.trace import TRACE_RELATIVE_ROOT

    assert tool_written_paths(tmp_path, RUN_A) == ()

    directory = tmp_path / TRACE_RELATIVE_ROOT / RUN_A
    directory.mkdir(parents=True)
    (directory / "trace.jsonl").write_text("not json\n", encoding="utf-8")

    assert tool_written_paths(tmp_path, RUN_A) == ()
