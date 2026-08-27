"""Filesystem behavior and boundary tests for the sole Stage B tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolResult
from proofcoder.tools.files import (
    MAX_LIST_ENTRIES,
    create_list_files_tool,
    is_sensitive_filename,
)
from proofcoder.tools.registry import ToolRegistry


def _dispatch(workspace: Path, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace))
    return registry.dispatch(
        ToolCall(
            id="list-1",
            function=FunctionCall(name="list_files", arguments=json.dumps(arguments)),
        )
    )


def _entries(result: ToolResult) -> list[dict[str, object]]:
    assert result.ok is True
    assert result.data is not None
    entries = result.data["entries"]
    assert isinstance(entries, list)
    return entries


def _error_code(result: ToolResult) -> str:
    assert result.error is not None
    return result.error.code


def test_normal_listing_is_sorted_and_workspace_relative(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")

    result = _dispatch(tmp_path, {})

    assert _entries(result) == [
        {"path": "a.txt", "type": "file"},
        {"path": "src", "type": "directory"},
        {"path": "src/main.py", "type": "file"},
        {"path": "z.txt", "type": "file"},
    ]
    assert result.data is not None
    assert result.data["queried_path"] == "."
    assert result.data["returned_count"] == 4


def test_max_depth_zero_and_one_have_explicit_semantics(tmp_path: Path) -> None:
    (tmp_path / "top.txt").write_text("", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "child.txt").write_text("", encoding="utf-8")

    assert _entries(_dispatch(tmp_path, {"max_depth": 0})) == []
    assert _entries(_dispatch(tmp_path, {"max_depth": 1})) == [
        {"path": "nested", "type": "directory"},
        {"path": "top.txt", "type": "file"},
    ]


def test_pattern_matches_case_sensitive_workspace_relative_posix_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "B.PY").write_text("", encoding="utf-8")
    (tmp_path / "root.py").write_text("", encoding="utf-8")

    result = _dispatch(tmp_path, {"pattern": "src/*.py"})

    assert _entries(result) == [{"path": "src/a.py", "type": "file"}]


def test_hidden_entries_are_opt_in_and_env_example_is_allowed(tmp_path: Path) -> None:
    (tmp_path / ".ordinary").write_text("", encoding="utf-8")
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=", encoding="utf-8")

    assert _entries(_dispatch(tmp_path, {})) == []
    assert _entries(_dispatch(tmp_path, {"include_hidden": True})) == [
        {"path": ".env.example", "type": "file"},
        {"path": ".ordinary", "type": "file"},
    ]


def test_sensitive_environment_and_private_key_names_are_always_filtered() -> None:
    assert is_sensitive_filename(".env") is True
    assert is_sensitive_filename(".env.local") is True
    assert is_sensitive_filename("id_rsa") is True
    assert is_sensitive_filename("server.pem") is True
    assert is_sensitive_filename(".env.example") is False


def test_default_ignored_directories_are_never_traversed(tmp_path: Path) -> None:
    for name in (".git", ".venv", "node_modules", "__pycache__", ".proofcoder"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "secret.txt").write_text("", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("", encoding="utf-8")

    result = _dispatch(tmp_path, {"include_hidden": True, "max_depth": 8})

    assert _entries(result) == [{"path": "visible.txt", "type": "file"}]


@pytest.mark.parametrize("requested", ["../", "../outside"])
def test_parent_escape_is_rejected(tmp_path: Path, requested: str) -> None:
    result = _dispatch(tmp_path, {"path": requested})

    assert _error_code(result) == "PATH_OUTSIDE_WORKSPACE"


def test_absolute_path_is_rejected_even_when_it_names_workspace(tmp_path: Path) -> None:
    result = _dispatch(tmp_path, {"path": str(tmp_path.resolve())})

    assert _error_code(result) == "PATH_OUTSIDE_WORKSPACE"


def test_missing_path_and_regular_file_have_distinct_errors(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("", encoding="utf-8")

    assert _error_code(_dispatch(tmp_path, {"path": "missing"})) == "PATH_NOT_FOUND"
    assert _error_code(_dispatch(tmp_path, {"path": "file.txt"})) == "NOT_A_DIRECTORY"


def test_querying_subdirectory_keeps_workspace_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")

    result = _dispatch(tmp_path, {"path": "src"})

    assert _entries(result) == [{"path": "src/main.py", "type": "file"}]
    assert result.data is not None
    assert result.data["queried_path"] == "src"


def test_external_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "outside-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    result = _dispatch(tmp_path, {"path": "outside-link"})

    assert _error_code(result) == "PATH_OUTSIDE_WORKSPACE"


def test_result_truncation_reports_complete_counts(tmp_path: Path) -> None:
    total = MAX_LIST_ENTRIES + 3
    for index in range(total):
        (tmp_path / f"item-{index:04}.txt").write_text("", encoding="utf-8")

    result = _dispatch(tmp_path, {"max_depth": 1})

    entries = _entries(result)
    assert len(entries) == MAX_LIST_ENTRIES
    assert result.data is not None
    assert result.data["returned_count"] == MAX_LIST_ENTRIES
    assert result.data["total_matched_count"] == total
    assert result.data["truncated_count"] == 3
    assert result.meta.truncated is True
