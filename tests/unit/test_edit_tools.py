"""Safety, fidelity, atomicity, and diff tests for Stage C2 edit tools."""

from __future__ import annotations

import codecs
import json
import stat
import sys
from pathlib import Path

import pytest

import proofcoder.safety.writes as writes
import proofcoder.tools.edit as edit
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import RiskLevel, ToolDefinition, ToolResult
from proofcoder.tools.edit import (
    MAX_CONTENT_SIZE_BYTES,
    MAX_DIFF_CHARACTERS,
    create_create_file_tool,
    create_replace_in_file_tool,
)
from proofcoder.tools.registry import ToolRegistry


def _dispatch(tool: ToolDefinition, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    return registry.dispatch(
        ToolCall(
            id="edit-1",
            function=FunctionCall(name=tool.name, arguments=json.dumps(arguments)),
        )
    )


def _create(workspace: Path, path: str, content: str) -> ToolResult:
    return _dispatch(create_create_file_tool(workspace), {"path": path, "content": content})


def _replace(
    workspace: Path,
    path: str,
    old_text: str,
    new_text: str,
    expected_replacements: int = 1,
) -> ToolResult:
    return _dispatch(
        create_replace_in_file_tool(workspace),
        {
            "path": path,
            "old_text": old_text,
            "new_text": new_text,
            "expected_replacements": expected_replacements,
        },
    )


def _data(result: ToolResult) -> dict[str, object]:
    assert result.ok is True
    assert result.data is not None
    return result.data


def _error_code(result: ToolResult) -> str:
    assert result.ok is False
    assert result.error is not None
    assert result.error.retryable is True
    return result.error.code


def _temporary_files(workspace: Path) -> list[Path]:
    return list(workspace.rglob(".*.proofcoder-*.tmp"))


@pytest.mark.parametrize("content", ["", "plain\n", "你好 ProofCoder\n"])
def test_create_file_writes_utf8_and_returns_diff(tmp_path: Path, content: str) -> None:
    result = _create(tmp_path, "created.txt", content)

    data = _data(result)
    assert (tmp_path / "created.txt").read_bytes() == content.encode("utf-8")
    assert data["path"] == "created.txt"
    assert data["bytes_written"] == len(content.encode("utf-8"))
    assert data["encoding"] == "utf-8"
    assert str(data["diff"]).startswith("--- /dev/null\n+++ b/created.txt\n")
    stats = data["diff_stats"]
    assert isinstance(stats, dict)
    assert stats["before_bytes"] == 0
    assert stats["after_bytes"] == len(content.encode("utf-8"))
    assert stats["replacement_count"] == 0
    assert _temporary_files(tmp_path) == []


def test_create_tool_is_marked_as_workspace_write(tmp_path: Path) -> None:
    tool = create_create_file_tool(tmp_path)

    assert tool.modifies_workspace is True
    assert tool.risk_level is RiskLevel.WRITE


@pytest.mark.parametrize(
    ("setup", "path", "expected"),
    [
        ("missing_parent", "missing/file.txt", "PARENT_NOT_FOUND"),
        ("file_parent", "parent/file.txt", "NOT_A_DIRECTORY"),
        ("existing_file", "target", "PATH_ALREADY_EXISTS"),
        ("existing_directory", "target", "PATH_ALREADY_EXISTS"),
    ],
)
def test_create_rejects_invalid_parent_and_existing_targets(
    tmp_path: Path,
    setup: str,
    path: str,
    expected: str,
) -> None:
    if setup == "file_parent":
        (tmp_path / "parent").write_text("parent", encoding="utf-8")
    elif setup == "existing_file":
        (tmp_path / "target").write_text("original", encoding="utf-8")
    elif setup == "existing_directory":
        (tmp_path / "target").mkdir()

    result = _create(tmp_path, path, "new")

    assert _error_code(result) == expected
    if setup == "existing_file":
        assert (tmp_path / "target").read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(
    "requested",
    ["..", "../outside.txt", "C:\\outside.txt", "/outside.txt"],
)
def test_create_rejects_paths_outside_workspace(tmp_path: Path, requested: str) -> None:
    assert _error_code(_create(tmp_path, requested, "new")) == "PATH_OUTSIDE_WORKSPACE"


def test_create_rejects_external_parent_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert _error_code(_create(tmp_path, "external/file.txt", "new")) == ("PATH_OUTSIDE_WORKSPACE")
    assert not (outside / "file.txt").exists()


@pytest.mark.parametrize("broken", [False, True])
def test_create_never_overwrites_symlink_targets(tmp_path: Path, broken: bool) -> None:
    destination = tmp_path / "destination.txt"
    if not broken:
        destination.write_text("original", encoding="utf-8")
    link = tmp_path / "target.txt"
    try:
        link.symlink_to(destination)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert _error_code(_create(tmp_path, link.name, "new")) == "PATH_ALREADY_EXISTS"
    if not broken:
        assert destination.read_text(encoding="utf-8") == "original"


def test_create_blocks_sensitive_path_but_allows_env_example(tmp_path: Path) -> None:
    assert _error_code(_create(tmp_path, ".env", "fictional")) == "SENSITIVE_PATH"

    assert _data(_create(tmp_path, ".env.example", "NAME=value"))["path"] == ".env.example"


def test_create_rejects_content_over_limit_without_side_effect(tmp_path: Path) -> None:
    result = _create(tmp_path, "large.txt", "x" * (MAX_CONTENT_SIZE_BYTES + 1))

    assert _error_code(result) == "CONTENT_TOO_LARGE"
    assert not (tmp_path / "large.txt").exists()
    assert _temporary_files(tmp_path) == []


def test_create_race_does_not_overwrite_new_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def target_appears(temporary: Path, target: Path) -> None:
        assert temporary.parent == target.parent
        target.write_text("external", encoding="utf-8")
        raise FileExistsError

    monkeypatch.setattr(edit, "commit_new_file", target_appears)

    result = _create(tmp_path, "race.txt", "agent")

    assert _error_code(result) == "PATH_ALREADY_EXISTS"
    assert (tmp_path / "race.txt").read_text(encoding="utf-8") == "external"
    assert _temporary_files(tmp_path) == []


def test_create_staging_and_commit_failures_leave_no_file_or_temporary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(writes.os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError()))
    staged = _create(tmp_path, "stage.txt", "content")
    assert _error_code(staged) == "ATOMIC_WRITE_ERROR"
    assert not (tmp_path / "stage.txt").exists()
    assert _temporary_files(tmp_path) == []

    monkeypatch.undo()
    monkeypatch.setattr(
        edit, "commit_new_file", lambda temporary, target: (_ for _ in ()).throw(OSError())
    )
    committed = _create(tmp_path, "commit.txt", "content")
    assert _error_code(committed) == "ATOMIC_WRITE_ERROR"
    assert not (tmp_path / "commit.txt").exists()
    assert _temporary_files(tmp_path) == []


def test_create_diff_is_bounded_without_truncating_written_content(tmp_path: Path) -> None:
    content = "x" * (MAX_DIFF_CHARACTERS * 2)

    result = _create(tmp_path, "wide.txt", content)

    data = _data(result)
    assert result.meta.truncated is True
    assert "diff truncated" in str(data["diff"])
    assert len(str(data["diff"])) <= MAX_DIFF_CHARACTERS
    assert str(data["diff"]).endswith(f"{content[-100:]}\n")
    assert (tmp_path / "wide.txt").read_text(encoding="utf-8") == content


def test_replace_once_multiple_and_delete(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("one old two old three", encoding="utf-8")

    first = _replace(tmp_path, target.name, "old", "new", expected_replacements=2)
    deleted = _replace(tmp_path, target.name, " two", "")

    assert _data(first)["replacements"] == 2
    assert _data(deleted)["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "one new new three"


def test_replace_rejects_oversized_arguments_without_reading_or_writing(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")

    result = _replace(tmp_path, target.name, "x" * (MAX_CONTENT_SIZE_BYTES + 1), "new")

    assert _error_code(result) == "CONTENT_TOO_LARGE"
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    ("old_text", "expected", "error"),
    [
        ("missing", 1, "MATCH_NOT_FOUND"),
        ("old", 1, "AMBIGUOUS_MATCH"),
        ("old", 3, "AMBIGUOUS_MATCH"),
    ],
)
def test_replace_match_failures_do_not_modify_file(
    tmp_path: Path,
    old_text: str,
    expected: int,
    error: str,
) -> None:
    target = tmp_path / "sample.txt"
    original = b"old and old"
    target.write_bytes(original)

    result = _replace(tmp_path, target.name, old_text, "new", expected)

    assert _error_code(result) == error
    if error == "AMBIGUOUS_MATCH":
        assert str(result.error.message).endswith(f"expected {expected} replacements")
    assert target.read_bytes() == original
    assert _temporary_files(tmp_path) == []


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_replace_matches_lf_arguments_and_preserves_uniform_newlines(
    tmp_path: Path,
    newline: str,
) -> None:
    target = tmp_path / "lines.txt"
    target.write_bytes(f"before{newline}old one{newline}old two{newline}after{newline}".encode())

    result = _replace(tmp_path, target.name, "old one\nold two", "new one\nnew two")

    assert result.ok is True
    assert target.read_bytes() == (
        f"before{newline}new one{newline}new two{newline}after{newline}".encode()
    )


def test_replace_preserves_bom_and_non_ascii_text(tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(codecs.BOM_UTF8 + "你好\n世界\n".encode())

    data = _data(_replace(tmp_path, target.name, "世界", "ProofCoder"))

    assert target.read_bytes() == codecs.BOM_UTF8 + "你好\nProofCoder\n".encode()
    assert data["encoding"] == "utf-8-sig"
    assert data["diff_stats"]["before_bytes"] == len(codecs.BOM_UTF8 + "你好\n世界\n".encode())
    assert data["diff_stats"]["after_bytes"] == len(codecs.BOM_UTF8 + "你好\nProofCoder\n".encode())


def test_replace_preserves_mixed_unmodified_newlines_and_uses_local_style(tmp_path: Path) -> None:
    target = tmp_path / "mixed.txt"
    target.write_bytes(b"head\r\nold one\nold two\rtail\r\n")

    result = _replace(tmp_path, target.name, "old one\nold two", "new one\nnew two")

    assert result.ok is True
    assert target.read_bytes() == b"head\r\nnew one\nnew two\rtail\r\n"


@pytest.mark.parametrize(
    ("original", "replacement", "expected"),
    [(b"value", "changed\n", b"changed"), (b"value\n", "changed", b"changed\n")],
)
def test_replace_preserves_trailing_newline_state(
    tmp_path: Path,
    original: bytes,
    replacement: str,
    expected: bytes,
) -> None:
    target = tmp_path / "tail.txt"
    target.write_bytes(original)

    assert _replace(tmp_path, target.name, "value", replacement).ok is True
    assert target.read_bytes() == expected


def test_replace_restores_terminal_newline_when_match_includes_it(tmp_path: Path) -> None:
    target = tmp_path / "tail.txt"
    target.write_bytes(b"value\r\n")

    assert _replace(tmp_path, target.name, "value\n", "changed").ok is True
    assert target.read_bytes() == b"changed\r\n"


@pytest.mark.parametrize(
    ("name", "raw", "error"),
    [
        ("binary.bin", b"text\x00binary", "BINARY_FILE"),
        ("legacy.txt", b"\xff\xfeplain", "DECODE_ERROR"),
        ("large.txt", b"x" * (MAX_CONTENT_SIZE_BYTES + 1), "FILE_TOO_LARGE"),
    ],
    ids=["binary", "non-utf8", "large"],
)
def test_replace_rejects_binary_non_utf8_and_large_files(
    tmp_path: Path,
    name: str,
    raw: bytes,
    error: str,
) -> None:
    target = tmp_path / name
    target.write_bytes(raw)

    assert _error_code(_replace(tmp_path, name, "plain", "new")) == error
    assert target.read_bytes() == raw


def test_replace_rejects_sensitive_and_external_symlink_paths(tmp_path: Path) -> None:
    sensitive = tmp_path / "id_rsa"
    sensitive.write_text("fictional", encoding="utf-8")
    assert _error_code(_replace(tmp_path, sensitive.name, "fictional", "new")) == ("SENSITIVE_PATH")

    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("old", encoding="utf-8")
    link = tmp_path / "external.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    assert _error_code(_replace(tmp_path, link.name, "old", "new")) == ("PATH_OUTSIDE_WORKSPACE")
    assert outside.read_text(encoding="utf-8") == "old"


def test_replace_detects_change_before_commit_and_preserves_external_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "race.txt"
    target.write_text("old", encoding="utf-8")

    def changed(path: Path, snapshot: object) -> bool:
        path.write_text("external", encoding="utf-8")
        return False

    monkeypatch.setattr(edit, "snapshot_still_matches", changed)

    result = _replace(tmp_path, target.name, "old", "new")

    assert _error_code(result) == "FILE_CHANGED"
    assert target.read_text(encoding="utf-8") == "external"
    assert _temporary_files(tmp_path) == []


def test_replace_staging_and_commit_failures_preserve_original_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(writes.os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError()))

    staged = _replace(tmp_path, target.name, "old", "new")
    assert _error_code(staged) == "ATOMIC_WRITE_ERROR"
    assert target.read_text(encoding="utf-8") == "old"
    assert _temporary_files(tmp_path) == []

    monkeypatch.undo()
    monkeypatch.setattr(
        edit,
        "commit_replacement",
        lambda temporary, destination: (_ for _ in ()).throw(OSError()),
    )
    committed = _replace(tmp_path, target.name, "old", "new")
    assert _error_code(committed) == "ATOMIC_WRITE_ERROR"
    assert target.read_text(encoding="utf-8") == "old"
    assert _temporary_files(tmp_path) == []


def test_replace_does_not_capture_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        edit,
        "stage_temporary_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        _replace(tmp_path, target.name, "old", "new")
    assert target.read_text(encoding="utf-8") == "old"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits are not reliable")
def test_replace_preserves_permission_bits(tmp_path: Path) -> None:
    target = tmp_path / "mode.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o640)

    assert _replace(tmp_path, target.name, "old", "new").ok is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_replace_returns_unified_diff_and_bounded_diff_keeps_full_write(tmp_path: Path) -> None:
    target = tmp_path / "diff.txt"
    original = "\n".join(f"old-{index}" for index in range(300)) + "\n"
    replacement = "\n".join(f"new-{index}" for index in range(300))
    target.write_bytes(original.encode())

    result = _replace(tmp_path, target.name, original.rstrip("\n"), replacement)

    data = _data(result)
    assert result.meta.truncated is True
    assert str(data["diff"]).startswith("--- a/diff.txt\n+++ b/diff.txt\n")
    assert "diff truncated" in str(data["diff"])
    assert len(str(data["diff"]).splitlines()) <= edit.MAX_DIFF_LINES
    assert len(str(data["diff"])) <= MAX_DIFF_CHARACTERS
    assert data["diff_stats"] == {
        "added_lines": 300,
        "removed_lines": 300,
        "before_bytes": len(original.encode()),
        "after_bytes": len((replacement + "\n").encode()),
        "replacement_count": 1,
    }
    assert target.read_bytes() == (replacement + "\n").encode()


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "file.txt", "content": "x", "overwrite": True},
        {"path": "", "content": "x"},
        {"path": "file.txt"},
    ],
)
def test_create_schema_rejects_unknown_empty_and_missing_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    assert _error_code(_dispatch(create_create_file_tool(tmp_path), arguments)) == (
        "INVALID_ARGUMENTS"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "file.txt", "old_text": "old", "new_text": "new", "extra": 1},
        {"path": "file.txt", "old_text": "", "new_text": "new"},
        {
            "path": "file.txt",
            "old_text": "old",
            "new_text": "new",
            "expected_replacements": True,
        },
    ],
)
def test_replace_schema_rejects_unknown_empty_and_boolean_arguments(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    (tmp_path / "file.txt").write_text("old", encoding="utf-8")

    assert _error_code(_dispatch(create_replace_in_file_tool(tmp_path), arguments)) == (
        "INVALID_ARGUMENTS"
    )
