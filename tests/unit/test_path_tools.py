"""Stage G path tools: deleting, moving, and creating directories."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.paths import (
    NO_CHECKPOINT_CODE,
    create_delete_path_tool,
    create_make_directory_tool,
    create_move_path_tool,
)
from proofcoder.tools.registry import ToolRegistry

# Written into a workspace .env so the tests can prove the tools refuse to touch it.
SENSITIVE_SENTINEL = "never-delete-this-value"


def _dispatch(tool: ToolDefinition, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    return registry.dispatch(
        ToolCall(
            id="call-1",
            function=FunctionCall(name=tool.name, arguments=json.dumps(arguments)),
        )
    )


def _delete(workspace: Path, path: str, *, checkpoint: bool = True) -> ToolResult:
    return _dispatch(
        create_delete_path_tool(workspace, checkpoint_available=checkpoint), {"path": path}
    )


def _move(workspace: Path, source: str, destination: str, *, checkpoint: bool = True) -> ToolResult:
    return _dispatch(
        create_move_path_tool(workspace, checkpoint_available=checkpoint),
        {"source": source, "destination": destination},
    )


def _mkdir(workspace: Path, path: str) -> ToolResult:
    return _dispatch(create_make_directory_tool(workspace), {"path": path})


def _code(result: ToolResult) -> str | None:
    return None if result.error is None else result.error.code


def _workspace(root: Path) -> Path:
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "empty").mkdir()
    (root / "full").mkdir()
    (root / "full" / "inside.txt").write_text("inside\n", encoding="utf-8")
    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}\n", encoding="utf-8")
    return root


def test_delete_removes_a_file_and_reports_what_it_freed(tmp_path: Path) -> None:
    _workspace(tmp_path)
    # Text mode writes CRLF on Windows, so the true size is the only correct
    # expectation; the tool reports what the filesystem holds, not what was typed.
    expected_bytes = (tmp_path / "keep.txt").stat().st_size

    result = _delete(tmp_path, "keep.txt")

    assert result.ok
    assert result.data == {"path": "keep.txt", "kind": "file", "bytes_freed": expected_bytes}
    assert not (tmp_path / "keep.txt").exists()


def test_delete_removes_an_empty_directory_but_never_a_full_one(tmp_path: Path) -> None:
    _workspace(tmp_path)

    empty = _delete(tmp_path, "empty")
    full = _delete(tmp_path, "full")

    assert empty.ok
    assert empty.data is not None and empty.data["kind"] == "directory"
    assert not (tmp_path / "empty").exists()
    # Recursive deletion is the one thing this tool must never do.
    assert _code(full) == "DIRECTORY_NOT_EMPTY"
    assert (tmp_path / "full" / "inside.txt").read_text(encoding="utf-8") == "inside\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links")
def test_delete_removes_a_link_without_following_it(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("host file\n", encoding="utf-8")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "escape").symlink_to(outside)

    result = _delete(root, "escape")

    assert result.ok
    assert result.data is not None and result.data["kind"] == "symlink"
    assert not (root / "escape").exists()
    assert outside.read_text(encoding="utf-8") == "host file\n"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (".env", "SENSITIVE_PATH"),
        ("id_rsa", "SENSITIVE_PATH"),
        (".proofcoder/runs", "SENSITIVE_PATH"),
        ("../escape.txt", "PATH_OUTSIDE_WORKSPACE"),
        ("/etc/passwd", "PATH_OUTSIDE_WORKSPACE"),
        ("missing.txt", "PATH_NOT_FOUND"),
        ("..", "PATH_OUTSIDE_WORKSPACE"),
    ],
)
def test_delete_refuses_paths_outside_its_remit(tmp_path: Path, path: str, code: str) -> None:
    _workspace(tmp_path)
    (tmp_path / "id_rsa").write_text("key material\n", encoding="utf-8")

    result = _delete(tmp_path, path)

    assert _code(result) == code
    assert (tmp_path / ".env").read_text(encoding="utf-8").endswith(f"{SENSITIVE_SENTINEL}\n")
    assert (tmp_path / "id_rsa").is_file()


def test_move_renames_a_file_and_a_directory(tmp_path: Path) -> None:
    _workspace(tmp_path)
    (tmp_path / "target").mkdir()

    renamed = _move(tmp_path, "keep.txt", "renamed.txt")
    moved = _move(tmp_path, "full", "target/full")

    assert renamed.ok
    assert renamed.data == {"source": "keep.txt", "destination": "renamed.txt", "kind": "file"}
    assert (tmp_path / "renamed.txt").read_text(encoding="utf-8") == "keep\n"
    assert moved.ok
    assert moved.data is not None and moved.data["kind"] == "directory"
    assert (tmp_path / "target" / "full" / "inside.txt").is_file()


def test_move_never_overwrites_its_destination(tmp_path: Path) -> None:
    _workspace(tmp_path)
    (tmp_path / "other.txt").write_text("other\n", encoding="utf-8")

    onto_file = _move(tmp_path, "keep.txt", "other.txt")
    onto_directory = _move(tmp_path, "keep.txt", "empty")
    onto_itself = _move(tmp_path, "keep.txt", "keep.txt")

    assert _code(onto_file) == "PATH_ALREADY_EXISTS"
    assert _code(onto_directory) == "PATH_ALREADY_EXISTS"
    assert _code(onto_itself) == "PATH_ALREADY_EXISTS"
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "other\n"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_move_refuses_sensitive_and_out_of_workspace_paths(tmp_path: Path) -> None:
    _workspace(tmp_path)

    source_sensitive = _move(tmp_path, ".env", "copy.txt")
    destination_sensitive = _move(tmp_path, "keep.txt", ".env.backup")
    escaping = _move(tmp_path, "keep.txt", "../escape.txt")

    assert _code(source_sensitive) == "SENSITIVE_PATH"
    assert _code(destination_sensitive) == "SENSITIVE_PATH"
    assert _code(escaping) == "PATH_OUTSIDE_WORKSPACE"
    assert (tmp_path / ".env").is_file()
    assert not (tmp_path.parent / "escape.txt").exists()


def test_move_requires_an_existing_parent_for_the_destination(tmp_path: Path) -> None:
    _workspace(tmp_path)

    result = _move(tmp_path, "keep.txt", "absent/renamed.txt")

    assert _code(result) == "PARENT_NOT_FOUND"
    assert (tmp_path / "keep.txt").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links")
def test_move_refuses_a_symbolic_link_source(tmp_path: Path) -> None:
    _workspace(tmp_path)
    (tmp_path / "link").symlink_to(tmp_path / "keep.txt")

    result = _move(tmp_path, "link", "moved")

    assert _code(result) == "NOT_A_FILE"
    assert (tmp_path / "link").is_symlink()


def test_make_directory_creates_missing_parents(tmp_path: Path) -> None:
    result = _mkdir(tmp_path, "one/two/three")

    assert result.ok
    assert result.data == {"path": "one/two/three", "created": True}
    assert (tmp_path / "one" / "two" / "three").is_dir()


def test_make_directory_is_content_with_an_existing_directory(tmp_path: Path) -> None:
    _workspace(tmp_path)

    result = _mkdir(tmp_path, "empty")

    assert result.ok
    assert result.data == {"path": "empty", "created": False}
    assert (tmp_path / "empty").is_dir()


def test_make_directory_refuses_a_path_held_by_something_else(tmp_path: Path) -> None:
    _workspace(tmp_path)

    over_file = _mkdir(tmp_path, "keep.txt")
    sensitive = _mkdir(tmp_path, ".env")
    runtime = _mkdir(tmp_path, ".proofcoder/checkpoints")
    escaping = _mkdir(tmp_path, "../escape")

    assert _code(over_file) == "PATH_ALREADY_EXISTS"
    assert _code(sensitive) == "SENSITIVE_PATH"
    assert _code(runtime) == "SENSITIVE_PATH"
    assert _code(escaping) == "PATH_OUTSIDE_WORKSPACE"
    assert (tmp_path / "keep.txt").is_file()
    assert not (tmp_path / ".proofcoder").exists()


def test_destructive_tools_refuse_a_run_without_a_checkpoint(tmp_path: Path) -> None:
    _workspace(tmp_path)

    deleted = _delete(tmp_path, "keep.txt", checkpoint=False)
    moved = _move(tmp_path, "keep.txt", "renamed.txt", checkpoint=False)

    assert _code(deleted) == NO_CHECKPOINT_CODE
    assert _code(moved) == NO_CHECKPOINT_CODE
    assert deleted.error is not None and deleted.error.retryable is False
    assert "--no-checkpoint" in str(deleted.error.message)
    # Nothing happened, so the workspace is exactly as it was.
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / "renamed.txt").exists()


def test_creating_a_directory_needs_no_checkpoint(tmp_path: Path) -> None:
    # make_directory only adds a path, which rollback removes cleanly, so it is not
    # gated the way the destructive tools are.
    result = _mkdir(tmp_path, "generated")

    assert result.ok
    assert (tmp_path / "generated").is_dir()


@pytest.mark.parametrize(
    ("tool_arguments", "factory"),
    [
        ({"path": "keep.txt", "recursive": True}, "delete"),
        ({}, "delete"),
        ({"source": "keep.txt"}, "move"),
        ({"source": "keep.txt", "destination": ""}, "move"),
    ],
)
def test_schemas_reject_unknown_empty_and_missing_arguments(
    tmp_path: Path,
    tool_arguments: dict[str, object],
    factory: str,
) -> None:
    _workspace(tmp_path)
    tool = (
        create_delete_path_tool(tmp_path, checkpoint_available=True)
        if factory == "delete"
        else create_move_path_tool(tmp_path, checkpoint_available=True)
    )

    assert _code(_dispatch(tool, tool_arguments)) == "INVALID_ARGUMENTS"
    assert (tmp_path / "keep.txt").is_file()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_move_keeps_the_file_mode(tmp_path: Path) -> None:
    script = tmp_path / "tool.sh"
    script.write_text("echo one\n", encoding="utf-8")
    script.chmod(0o750)

    result = _move(tmp_path, "tool.sh", "bin.sh")

    assert result.ok
    assert stat.S_IMODE((tmp_path / "bin.sh").stat().st_mode) == 0o750


def test_tools_declare_that_they_modify_the_workspace(tmp_path: Path) -> None:
    tools = [
        create_delete_path_tool(tmp_path, checkpoint_available=True),
        create_move_path_tool(tmp_path, checkpoint_available=True),
        create_make_directory_tool(tmp_path),
    ]

    assert all(tool.modifies_workspace for tool in tools)
    assert all(tool.risk_level.value == "write" for tool in tools)


def test_filesystem_failures_become_structured_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os as os_module

    _workspace(tmp_path)
    original_unlink = Path.unlink
    original_rename = os_module.rename

    def refuse_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("busy")

    def refuse_rename(source: object, destination: object) -> None:
        raise OSError("cross-device")

    monkeypatch.setattr(Path, "unlink", refuse_unlink)
    deleted = _delete(tmp_path, "keep.txt")
    monkeypatch.setattr(Path, "unlink", original_unlink)

    monkeypatch.setattr("proofcoder.tools.paths.os.rename", refuse_rename)
    moved = _move(tmp_path, "keep.txt", "renamed.txt")
    monkeypatch.setattr("proofcoder.tools.paths.os.rename", original_rename)

    assert _code(deleted) == "DELETE_ERROR"
    assert _code(moved) == "MOVE_ERROR"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / "renamed.txt").exists()


def test_make_directory_reports_a_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", refuse_mkdir)
    result = _mkdir(tmp_path, "generated")

    assert _code(result) == "DIRECTORY_CREATE_ERROR"


def test_make_directory_rejects_an_escaping_path_with_missing_parents(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    escaping = _mkdir(root, "../outside/nested")
    dotted = _mkdir(root, "one/../..")

    assert _code(escaping) == "PATH_OUTSIDE_WORKSPACE"
    assert _code(dotted) == "PATH_OUTSIDE_WORKSPACE"
    assert not (tmp_path / "outside").exists()


def test_make_directory_rejects_a_sensitive_path_with_missing_parents(tmp_path: Path) -> None:
    result = _mkdir(tmp_path, ".proofcoder/checkpoints/extra")

    assert _code(result) == "SENSITIVE_PATH"
    assert not (tmp_path / ".proofcoder").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links")
def test_make_directory_refuses_a_link_standing_where_the_directory_would_be(
    tmp_path: Path,
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real")

    result = _mkdir(tmp_path, "linked")

    assert _code(result) == "PATH_ALREADY_EXISTS"
    assert (tmp_path / "linked").is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX named pipes")
def test_delete_refuses_a_special_file(tmp_path: Path) -> None:
    import os as os_module

    os_module.mkfifo(tmp_path / "pipe")

    result = _delete(tmp_path, "pipe")

    assert _code(result) == "NOT_A_FILE"
    assert (tmp_path / "pipe").exists()


def test_move_refuses_a_destination_that_appears_after_it_was_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.tools.paths as paths_module

    _workspace(tmp_path)
    original = paths_module.resolve_workspace_new_file

    def resolve_then_create(workspace: Path, requested: str) -> tuple[Path, str]:
        target, relative = original(workspace, requested)
        # Stand in for another process winning the race between check and rename.
        target.write_text("written by someone else\n", encoding="utf-8")
        return target, relative

    monkeypatch.setattr(paths_module, "resolve_workspace_new_file", resolve_then_create)
    result = _move(tmp_path, "keep.txt", "renamed.txt")

    assert _code(result) == "PATH_ALREADY_EXISTS"
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (tmp_path / "renamed.txt").read_text(encoding="utf-8") == "written by someone else\n"
