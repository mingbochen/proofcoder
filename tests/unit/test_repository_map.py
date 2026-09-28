"""Offline tests for the read-only repository map tool."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolResult
from proofcoder.tools.registry import ToolRegistry
from proofcoder.tools.repository import (
    MAX_MAP_FILES,
    MAX_PARSE_BYTES,
    MAX_SYMBOLS_PER_FILE,
    create_repository_map_tool,
)

SENSITIVE_SENTINEL = "never-parse-this-credential-file"


def _run(root: Path, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(create_repository_map_tool(root))
    prepared = registry.prepare(
        ToolCall(
            id="c",
            function=FunctionCall(name="repository_map", arguments=json.dumps(arguments)),
        )
    )
    if isinstance(prepared, ToolResult):
        return prepared
    return registry.execute(prepared)


def _map(root: Path, **arguments: object) -> dict[str, object]:
    result = _run(root, dict(arguments))
    assert result.ok, result.error
    assert result.data is not None
    return result.data


def _paths(data: dict[str, object]) -> list[str]:
    files = data["files"]
    assert isinstance(files, list)
    return [str(item["path"]) for item in files]


def _entry(data: dict[str, object], path: str) -> dict[str, object]:
    files = data["files"]
    assert isinstance(files, list)
    return next(item for item in files if item["path"] == path)


def test_python_symbols_and_line_numbers_are_reported(tmp_path: Path) -> None:
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / "cart.py").write_text(
        "import math\n\n\n"
        "def total(items):\n    return sum(items)\n\n\n"
        "class Cart:\n"
        "    def add(self, item):\n        pass\n\n"
        "    async def checkout(self):\n        pass\n\n\n"
        "async def refresh():\n    pass\n",
        encoding="utf-8",
    )

    entry = _entry(_map(tmp_path), "shop/cart.py")

    assert entry["symbols"] == [
        {"kind": "function", "name": "total", "line": 4},
        {
            "kind": "class",
            "name": "Cart",
            "line": 8,
            "methods": [{"name": "add", "line": 9}, {"name": "checkout", "line": 12}],
        },
        {"kind": "function", "name": "refresh", "line": 16},
    ]


def test_parsing_never_executes_the_source(tmp_path: Path) -> None:
    """ast builds a tree and runs nothing; a side effect here would prove otherwise."""

    marker = tmp_path / "executed.txt"
    (tmp_path / "boobytrap.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n\ndef present():\n    pass\n",
        encoding="utf-8",
    )

    entry = _entry(_map(tmp_path), "boobytrap.py")

    assert not marker.exists()
    assert entry["symbols"] == [{"kind": "function", "name": "present", "line": 3}]


def test_other_languages_are_listed_without_guessed_symbols(tmp_path: Path) -> None:
    # Bytes, not text: text mode on Windows writes CRLF and the size would differ.
    (tmp_path / "app.js").write_bytes(b"function run() {}\n")

    entry = _entry(_map(tmp_path), "app.js")

    assert entry == {"path": "app.js", "size": 18}


def test_a_file_that_does_not_parse_is_listed_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "fine.py").write_text("def fine():\n    pass\n", encoding="utf-8")

    data = _map(tmp_path)

    assert _entry(data, "broken.py")["note"] == "does not parse"
    assert _entry(data, "fine.py")["symbols"] == [{"kind": "function", "name": "fine", "line": 1}]


def test_non_utf8_and_oversized_files_are_listed_but_not_parsed(tmp_path: Path) -> None:
    (tmp_path / "latin.py").write_bytes(b"x = '\xe9'\n")
    (tmp_path / "huge.py").write_text("x = 1\n" * (MAX_PARSE_BYTES // 6 + 10), encoding="utf-8")

    data = _map(tmp_path)

    assert _entry(data, "latin.py")["note"] == "not UTF-8 text"
    assert _entry(data, "huge.py")["note"] == "too large to parse"


def test_pathologically_nested_source_does_not_break_the_map(tmp_path: Path) -> None:
    (tmp_path / "nested.py").write_text("x = " + "(" * 5000 + ")" * 5000 + "\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def ok():\n    pass\n", encoding="utf-8")

    data = _map(tmp_path)

    assert _entry(data, "nested.py").get("symbols") is None
    assert _entry(data, "ok.py")["symbols"] == [{"kind": "function", "name": "ok", "line": 1}]


def test_sensitive_hidden_and_ignored_entries_are_omitted(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"TOKEN={SENSITIVE_SENTINEL}\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
    for ignored in (".git", "node_modules", "__pycache__", ".proofcoder", ".venv"):
        (tmp_path / ignored).mkdir()
        (tmp_path / ignored / "inside.py").write_text("def inside():\n    pass\n")
    (tmp_path / "visible.py").write_text("def visible():\n    pass\n", encoding="utf-8")

    data = _map(tmp_path)

    assert _paths(data) == ["visible.py"]
    assert SENSITIVE_SENTINEL not in repr(data)


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_symbolic_links_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def secret():\n    pass\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "real.py").write_text("def real():\n    pass\n", encoding="utf-8")
    (workspace / "linked_dir").symlink_to(outside, target_is_directory=True)
    (workspace / "linked.py").symlink_to(outside / "secret.py")

    data = _map(workspace)

    assert _paths(data) == ["real.py"]


def test_depth_limits_the_walk(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (tmp_path / "top.py").write_text("", encoding="utf-8")
    (deep / "deep.py").write_text("", encoding="utf-8")

    shallow = _map(tmp_path, max_depth=2)
    full = _map(tmp_path, max_depth=6)

    assert _paths(shallow) == ["top.py"]
    assert "a/b/c/deep.py" in _paths(full)


def test_symbols_can_be_turned_off(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_bytes(b"def f():\n    pass\n")

    data = _map(tmp_path, include_symbols=False)

    assert _entry(data, "m.py") == {"path": "m.py", "size": 18}
    assert data["symbols_parsed"] is False


def test_the_file_and_symbol_limits_report_what_they_cut(tmp_path: Path) -> None:
    for index in range(MAX_MAP_FILES + 5):
        (tmp_path / f"f{index:04d}.txt").write_text("", encoding="utf-8")
    many = "\n".join(f"def f{index}():\n    pass\n" for index in range(MAX_SYMBOLS_PER_FILE + 7))
    (tmp_path / "aaa_many.py").write_text(many, encoding="utf-8")

    data = _map(tmp_path)

    assert data["returned_count"] == MAX_MAP_FILES
    assert data["truncated_count"] == 6
    entry = _entry(data, "aaa_many.py")
    assert len(entry["symbols"]) == MAX_SYMBOLS_PER_FILE  # type: ignore[arg-type]
    assert entry["symbols_truncated"] == 7


def test_a_subdirectory_can_be_mapped_and_escape_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "other.py").write_text("", encoding="utf-8")

    assert _paths(_map(tmp_path, path="pkg")) == ["pkg/mod.py"]

    assert not _run(tmp_path, {"path": "../"}).ok
