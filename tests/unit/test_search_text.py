"""Offline semantics and subprocess-safety tests for search_text."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import proofcoder.tools.search as search
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolResult
from proofcoder.tools.registry import ToolRegistry
from proofcoder.tools.search import (
    MAX_MATCH_LINE_CHARS,
    MAX_SEARCH_FILE_SIZE_BYTES,
    create_search_text_tool,
    minimal_subprocess_environment,
)


def _dispatch(workspace: Path, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(create_search_text_tool(workspace))
    return registry.dispatch(
        ToolCall(
            id="search-1",
            function=FunctionCall(name="search_text", arguments=json.dumps(arguments)),
        )
    )


def _data(result: ToolResult) -> dict[str, object]:
    assert result.ok is True
    assert result.data is not None
    return result.data


def _matches(result: ToolResult) -> list[dict[str, object]]:
    matches = _data(result)["matches"]
    assert isinstance(matches, list)
    return matches


def _error_code(result: ToolResult) -> str:
    assert result.error is not None
    return result.error.code


@pytest.fixture(autouse=True)
def _force_python_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda name: None)


def test_fixed_search_is_case_insensitive_sorted_and_workspace_relative(tmp_path: Path) -> None:
    (tmp_path / "z.py").write_text("AgentLoop\nnone", encoding="utf-8")
    (tmp_path / "a.py").write_text("agentloop\nAGENTLOOP", encoding="utf-8")

    result = _dispatch(tmp_path, {"query": "AgentLoop"})

    assert _matches(result) == [
        {
            "path": "a.py",
            "line_number": 1,
            "line": "agentloop",
            "line_truncated": False,
        },
        {
            "path": "a.py",
            "line_number": 2,
            "line": "AGENTLOOP",
            "line_truncated": False,
        },
        {
            "path": "z.py",
            "line_number": 1,
            "line": "AgentLoop",
            "line_truncated": False,
        },
    ]
    assert _data(result)["queried_path"] == "."
    assert _data(result)["returned_count"] == 3
    assert result.meta.truncated is False


def test_regex_case_sensitivity_path_and_glob_have_shared_semantics(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "match.py").write_text("AgentLoop\nagentloop\nAgent123", encoding="utf-8")
    (source / "skip.txt").write_text("Agent999", encoding="utf-8")

    result = _dispatch(
        tmp_path,
        {
            "query": r"Agent(?:Loop|\d+)",
            "path": "src",
            "glob": "*.py",
            "regex": True,
            "case_sensitive": True,
        },
    )

    assert [(item["path"], item["line_number"]) for item in _matches(result)] == [
        ("src/match.py", 1),
        ("src/match.py", 3),
    ]


def test_invalid_regex_and_zero_matches_are_distinct(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("present", encoding="utf-8")

    invalid = _dispatch(tmp_path, {"query": "[", "regex": True})
    empty = _dispatch(tmp_path, {"query": "absent"})

    assert _error_code(invalid) == "INVALID_PATTERN"
    assert empty.ok is True
    assert _matches(empty) == []
    assert _data(empty)["returned_count"] == 0


def test_global_limit_and_long_match_line_are_reported(tmp_path: Path) -> None:
    long_line = "needle" + "x" * (MAX_MATCH_LINE_CHARS + 10)
    (tmp_path / "many.txt").write_text(
        "\n".join((long_line, "needle two", "needle three")),
        encoding="utf-8",
    )

    result = _dispatch(tmp_path, {"query": "needle", "max_results": 2})

    matches = _matches(result)
    assert len(matches) == 2
    assert len(str(matches[0]["line"])) == MAX_MATCH_LINE_CHARS
    assert matches[0]["line_truncated"] is True
    assert result.meta.truncated is True
    assert _data(result)["more_matches_available"] is True


def test_skips_binary_large_sensitive_and_ignored_files(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("needle", encoding="utf-8")
    (tmp_path / ".env.example").write_text("needle safe-example", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"needle\x00binary")
    (tmp_path / "large.txt").write_bytes(b"needle" + b"x" * MAX_SEARCH_FILE_SIZE_BYTES)
    (tmp_path / ".env.local").write_text("needle fictional", encoding="utf-8")
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "package.js").write_text("needle", encoding="utf-8")

    result = _dispatch(tmp_path, {"query": "needle"})

    assert [item["path"] for item in _matches(result)] == [".env.example", "visible.txt"]


def test_python_fallback_recurses_into_normal_directories(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)
    (nested / "module.py").write_text("needle", encoding="utf-8")

    assert [item["path"] for item in _matches(_dispatch(tmp_path, {"query": "needle"}))] == [
        "src/package/module.py"
    ]


def test_external_symlinks_are_not_read(tmp_path: Path) -> None:
    outside_file = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside_file.write_text("needle", encoding="utf-8")
    outside_directory = tmp_path.parent / f"{tmp_path.name}-outside-dir"
    outside_directory.mkdir()
    (outside_directory / "nested.txt").write_text("needle", encoding="utf-8")
    try:
        (tmp_path / "file-link").symlink_to(outside_file)
        (tmp_path / "dir-link").symlink_to(outside_directory, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    (tmp_path / "visible.txt").write_text("needle", encoding="utf-8")

    assert [item["path"] for item in _matches(_dispatch(tmp_path, {"query": "needle"}))] == [
        "visible.txt"
    ]
    assert _error_code(_dispatch(tmp_path, {"query": "needle", "path": "dir-link"})) == (
        "PATH_OUTSIDE_WORKSPACE"
    )


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({}, "INVALID_ARGUMENTS"),
        ({"query": ""}, "INVALID_ARGUMENTS"),
        ({"query": "x", "max_results": True}, "INVALID_ARGUMENTS"),
        ({"query": "x", "max_results": 0}, "INVALID_ARGUMENTS"),
        ({"query": "x", "max_results": 201}, "INVALID_ARGUMENTS"),
        ({"query": "x", "unknown": 1}, "INVALID_ARGUMENTS"),
        ({"query": "x", "path": "missing"}, "PATH_NOT_FOUND"),
        ({"query": "x", "path": "../outside"}, "PATH_OUTSIDE_WORKSPACE"),
    ],
)
def test_argument_and_path_failures_are_stable(
    tmp_path: Path,
    arguments: dict[str, object],
    code: str,
) -> None:
    assert _error_code(_dispatch(tmp_path, arguments)) == code


def test_file_and_absolute_search_paths_are_rejected_distinctly(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")

    assert _error_code(_dispatch(tmp_path, {"query": "needle", "path": "code.py"})) == (
        "NOT_A_DIRECTORY"
    )
    assert _error_code(_dispatch(tmp_path, {"query": "needle", "path": str(tmp_path)})) == (
        "PATH_OUTSIDE_WORKSPACE"
    )


def test_ripgrep_uses_argv_no_shell_no_config_and_filtered_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_text("AgentLoop", encoding="utf-8")
    monkeypatch.setattr(search.shutil, "which", lambda name: "rg-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fictional-test-key")
    captured: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        captured["arguments"] = arguments
        captured.update(kwargs)
        event = {
            "type": "match",
            "data": {"line_number": 1, "lines": {"text": "AgentLoop\n"}},
        }
        return SimpleNamespace(returncode=0, stdout=json.dumps(event), stderr="")

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    result = _dispatch(tmp_path, {"query": "AgentLoop", "case_sensitive": True})

    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert "--no-config" in arguments
    assert "--fixed-strings" in arguments
    assert arguments[-3:] == ["--", "AgentLoop", "code.py"]
    assert captured["shell"] is False
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "DEEPSEEK_API_KEY" not in environment
    assert _matches(result)[0]["line"] == "AgentLoop"


def test_ripgrep_and_python_fallback_return_the_same_match_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_bytes(b"alpha\nNeedle\nneedle\n")
    python_result = _dispatch(tmp_path, {"query": "needle"})
    monkeypatch.setattr(search.shutil, "which", lambda name: "rg-test")

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        events = [
            {
                "type": "match",
                "data": {"line_number": 2, "lines": {"text": "Needle\n"}},
            },
            {
                "type": "match",
                "data": {"line_number": 3, "lines": {"text": "needle\n"}},
            },
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    monkeypatch.setattr(search.subprocess, "run", fake_run)
    ripgrep_result = _dispatch(tmp_path, {"query": "needle"})

    assert _matches(ripgrep_result) == _matches(python_result)
    assert _data(ripgrep_result)["returned_count"] == _data(python_result)["returned_count"]
    assert ripgrep_result.meta.truncated is python_result.meta.truncated


def test_minimal_environment_only_fetches_allowed_non_secret_values() -> None:
    class GuardedEnvironment(dict[str, str]):
        def __getitem__(self, key: str) -> str:
            if key == "API_TOKEN":
                raise AssertionError("secret value must not be read")
            return super().__getitem__(key)

    environment = GuardedEnvironment(
        {
            "PATH": "tools",
            "TEMP": "temporary",
            "API_TOKEN": "fictional-test-token",
            "ORDINARY": "omitted",
        }
    )

    assert minimal_subprocess_environment(environment) == {
        "PATH": "tools",
        "TEMP": "temporary",
    }


def test_ripgrep_failure_returns_search_error_without_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(search.shutil, "which", lambda name: "rg-test")
    monkeypatch.setattr(
        search.subprocess,
        "run",
        lambda arguments, **kwargs: SimpleNamespace(
            returncode=3,
            stdout="",
            stderr="fictional-sensitive-stderr",
        ),
    )

    result = _dispatch(tmp_path, {"query": "needle"})

    assert _error_code(result) == "SEARCH_ERROR"
    assert "fictional-sensitive-stderr" not in result.to_json()
