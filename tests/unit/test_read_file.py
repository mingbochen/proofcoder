"""Boundary, decoding, newline, and truncation tests for read_file."""

from __future__ import annotations

import codecs
import json
from pathlib import Path

import pytest

from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolResult
from proofcoder.tools.files import (
    MAX_FILE_SIZE_BYTES,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    create_read_file_tool,
)
from proofcoder.tools.registry import ToolRegistry


def _dispatch(workspace: Path, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(create_read_file_tool(workspace))
    return registry.dispatch(
        ToolCall(
            id="read-1",
            function=FunctionCall(name="read_file", arguments=json.dumps(arguments)),
        )
    )


def _data(result: ToolResult) -> dict[str, object]:
    assert result.ok is True
    assert result.data is not None
    return result.data


def _error_code(result: ToolResult) -> str:
    assert result.error is not None
    return result.error.code


def test_reads_inclusive_range_with_line_numbers_and_metadata(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_bytes(b"alpha\nbeta\ngamma")

    result = _dispatch(tmp_path, {"path": "sample.py", "start_line": 2, "end_line": 3})

    data = _data(result)
    assert data["path"] == "sample.py"
    assert data["content"] == "2: beta\n3: gamma"
    assert data["total_lines"] == 3
    assert data["start_line"] == 2
    assert data["end_line"] == 3
    assert data["returned_line_count"] == 2
    assert data["encoding"] == "utf-8"
    assert data["newline_style"] == "lf"
    assert result.meta.truncated is False


@pytest.mark.parametrize(
    ("raw", "style", "content"),
    [
        (b"one\ntwo\n", "lf", "1: one\n2: two"),
        (b"one\r\ntwo\r\n", "crlf", "1: one\n2: two"),
        (b"one\rtwo\r", "cr", "1: one\n2: two"),
        (b"one\r\ntwo\nthree\r", "mixed", "1: one\n2: two\n3: three"),
        (b"one", "none", "1: one"),
    ],
)
def test_reports_newline_style_and_handles_trailing_newline(
    tmp_path: Path,
    raw: bytes,
    style: str,
    content: str,
) -> None:
    (tmp_path / "lines.txt").write_bytes(raw)

    data = _data(_dispatch(tmp_path, {"path": "lines.txt"}))

    assert data["newline_style"] == style
    assert data["content"] == content


def test_utf8_bom_is_removed_and_reported(tmp_path: Path) -> None:
    (tmp_path / "bom.txt").write_bytes(codecs.BOM_UTF8 + "你好\n".encode())

    data = _data(_dispatch(tmp_path, {"path": "bom.txt"}))

    assert data["encoding"] == "utf-8-sig"
    assert data["content"] == "1: 你好"


def test_empty_file_is_a_successful_empty_segment(tmp_path: Path) -> None:
    (tmp_path / "empty.txt").write_bytes(b"")

    data = _data(_dispatch(tmp_path, {"path": "empty.txt"}))

    assert data["content"] == ""
    assert data["total_lines"] == 0
    assert data["start_line"] is None
    assert data["end_line"] is None


def test_invalid_ranges_are_recoverable(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("only", encoding="utf-8")

    reversed_result = _dispatch(
        tmp_path,
        {"path": "one.txt", "start_line": 2, "end_line": 1},
    )
    beyond_result = _dispatch(tmp_path, {"path": "one.txt", "start_line": 2})

    assert _error_code(reversed_result) == "INVALID_RANGE"
    assert _error_code(beyond_result) == "INVALID_RANGE"


def test_line_and_byte_limits_report_truncation(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text(
        "\n".join(f"line-{index}" for index in range(MAX_READ_LINES + 1)),
        encoding="utf-8",
    )
    (tmp_path / "wide.txt").write_text("x" * (MAX_READ_BYTES + 100), encoding="utf-8")

    line_result = _dispatch(tmp_path, {"path": "many.txt"})
    byte_result = _dispatch(tmp_path, {"path": "wide.txt"})

    assert line_result.meta.truncated is True
    assert _data(line_result)["returned_line_count"] == MAX_READ_LINES
    assert byte_result.meta.truncated is True
    byte_data = _data(byte_result)
    assert byte_data["returned_bytes"] == MAX_READ_BYTES
    assert len(str(byte_data["content"]).encode()) == MAX_READ_BYTES


def test_binary_large_and_non_utf8_files_have_distinct_errors(tmp_path: Path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"text\x00binary")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-text")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_FILE_SIZE_BYTES + 1))
    (tmp_path / "legacy.txt").write_bytes(b"\xff\xfeplain")

    assert _error_code(_dispatch(tmp_path, {"path": "binary.bin"})) == "BINARY_FILE"
    assert _error_code(_dispatch(tmp_path, {"path": "image.png"})) == "BINARY_FILE"
    assert _error_code(_dispatch(tmp_path, {"path": "large.txt"})) == "FILE_TOO_LARGE"
    assert _error_code(_dispatch(tmp_path, {"path": "legacy.txt"})) == "DECODE_ERROR"


@pytest.mark.parametrize(
    "name",
    [".env", ".ENV.Local", "id_rsa", "ID_ED25519", "private.pem", "client.CRT"],
)
def test_sensitive_paths_are_rejected_case_insensitively(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("fictional-test-value", encoding="utf-8")

    result = _dispatch(tmp_path, {"path": name})

    assert _error_code(result) == "SENSITIVE_PATH"
    assert "fictional-test-value" not in result.to_json()


@pytest.mark.parametrize("name", [".env.example", "keyboard.py", "tokenizer.py"])
def test_safe_similar_names_are_readable(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("safe", encoding="utf-8")

    assert _data(_dispatch(tmp_path, {"path": name}))["content"] == "1: safe"


def test_missing_directory_absolute_and_parent_paths_are_distinct(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()

    assert _error_code(_dispatch(tmp_path, {"path": "missing"})) == "PATH_NOT_FOUND"
    assert _error_code(_dispatch(tmp_path, {"path": "folder"})) == "NOT_A_FILE"
    assert _error_code(_dispatch(tmp_path, {"path": "../outside"})) == ("PATH_OUTSIDE_WORKSPACE")
    assert _error_code(_dispatch(tmp_path, {"path": str(tmp_path)})) == ("PATH_OUTSIDE_WORKSPACE")


def test_external_file_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert _error_code(_dispatch(tmp_path, {"path": link.name})) == ("PATH_OUTSIDE_WORKSPACE")


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "file.txt", "start_line": True},
        {"path": "file.txt", "end_line": False},
        {"path": "file.txt", "unknown": 1},
        {"path": ""},
    ],
)
def test_schema_rejects_boolean_integer_unknown_and_empty_path(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    (tmp_path / "file.txt").write_text("text", encoding="utf-8")

    assert _error_code(_dispatch(tmp_path, arguments)) == "INVALID_ARGUMENTS"
