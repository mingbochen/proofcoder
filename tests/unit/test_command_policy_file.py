"""Offline tests for loading, freezing, and applying a project command policy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import proofcoder.safety.commands as command_policy
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.safety.commands import (
    BUILTIN_EXECUTABLE_NAMES,
    CommandPolicyError,
    load_project_command_policy,
    prepare_command,
)
from proofcoder.safety.policy import (
    POLICY_FILENAME,
    CommandDecision,
    CommandPolicyFileError,
    is_command_policy_path,
)
from proofcoder.tools.base import ToolResult
from proofcoder.tools.edit import create_create_file_tool, create_replace_in_file_tool
from proofcoder.tools.files import create_read_file_tool
from proofcoder.tools.paths import create_delete_path_tool, create_move_path_tool
from proofcoder.tools.registry import ToolRegistry

VALID_POLICY = """
schema_version = 1

[[command]]
executable = "make"
subcommands = ["test", "check"]
options = ["--keep-going"]
decision = "allow"
kind = "test"

[[command]]
executable = "node"
options = ["--test"]
decision = "confirm"
kind = "test"
"""


@pytest.fixture(autouse=True)
def _stable_executable_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every declared executable to this interpreter, never running it."""

    monkeypatch.setattr(
        command_policy.shutil,
        "which",
        lambda executable, *, path: str(Path(sys.executable).resolve()),
    )


def _environment() -> dict[str, str]:
    return {"PATH": str(Path(sys.executable).resolve().parent)}


def _write_policy(workspace: Path, body: str = VALID_POLICY) -> Path:
    path = workspace / POLICY_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def _load(workspace: Path, body: str = VALID_POLICY) -> object:
    return load_project_command_policy(_write_policy(workspace, body), workspace=workspace)


def _assert_policy_error(workspace: Path, body: str, code: str) -> None:
    with pytest.raises(CommandPolicyFileError) as captured:
        _load(workspace, body)
    assert captured.value.code == code


def test_a_valid_policy_freezes_its_entries_source_and_digest(tmp_path: Path) -> None:
    policy = _load(tmp_path)

    assert policy.source == POLICY_FILENAME
    assert len(policy.digest) == 64
    assert [entry.executable for entry in policy.entries] == ["make", "node"]
    make = policy.entry_for("make")
    assert make is not None
    assert make.decision is CommandDecision.ALLOW
    assert make.kind == "test"
    assert make.subcommands == ("test", "check")
    node = policy.entry_for("node")
    assert node is not None
    assert node.decision is CommandDecision.CONFIRM


def test_without_a_policy_the_decision_is_exactly_what_it_was(tmp_path: Path) -> None:
    """The mechanism is opt-in, so nothing changes for a caller that never opts in."""

    with pytest.raises(CommandPolicyError) as captured:
        prepare_command(tmp_path, {"argv": ["make", "test"]}, environ=_environment())

    assert captured.value.code == "COMMAND_BLOCKED"


def test_a_policy_file_on_disk_does_nothing_until_it_is_named(tmp_path: Path) -> None:
    """Repository content cannot authorize itself; only the caller can."""

    _write_policy(tmp_path)

    with pytest.raises(CommandPolicyError) as captured:
        prepare_command(tmp_path, {"argv": ["make", "test"]}, environ=_environment())

    assert captured.value.code == "COMMAND_BLOCKED"


def test_a_named_policy_supplies_the_declared_decision_and_kind(tmp_path: Path) -> None:
    policy = _load(tmp_path)

    allowed = prepare_command(
        tmp_path,
        {"argv": ["make", "test", "--keep-going"]},
        environ=_environment(),
        policy=policy,
    )
    confirmable = prepare_command(
        tmp_path,
        {"argv": ["node", "--test"]},
        environ=_environment(),
        policy=policy,
    )

    assert allowed.decision is CommandDecision.ALLOW
    assert allowed.command_kind == "test"
    assert allowed.decision_source == "policy"
    assert confirmable.decision is CommandDecision.CONFIRM
    assert confirmable.decision_source == "policy"


@pytest.mark.parametrize(
    "argv",
    [
        ["make"],
        ["make", "install"],
        ["make", "test", "--jobs"],
        ["make", "test", "../outside"],
    ],
)
def test_a_declaration_is_never_a_blanket_allowance(tmp_path: Path, argv: list[str]) -> None:
    """Section 7.6 still requires the subcommand and the options to be checked."""

    policy = _load(tmp_path)

    with pytest.raises(CommandPolicyError):
        prepare_command(tmp_path, {"argv": argv}, environ=_environment(), policy=policy)


@pytest.mark.parametrize("executable", ["git", "python", "pytest", "ruff", "npm", "bash", "rm"])
def test_a_policy_may_not_redeclare_any_built_in_name(tmp_path: Path, executable: str) -> None:
    """Extending default deny is allowed; replacing a built-in judgment is not."""

    assert executable in BUILTIN_EXECUTABLE_NAMES
    body = f"""
schema_version = 1

[[command]]
executable = "{executable}"
decision = "allow"
kind = "test"
"""

    _assert_policy_error(tmp_path, body, "POLICY_SHADOWS_BUILTIN")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            "schema_version = 2\n[[command]]\nexecutable='make'\ndecision='allow'\nkind='test'\n",
            "POLICY_INVALID",
        ),
        ("schema_version = 1\n", "POLICY_INVALID"),
        (
            "schema_version = 1\nextra = 1\n[[command]]\nexecutable='make'\n"
            "decision='allow'\nkind='test'\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='make'\ndecision='deny'\nkind='test'\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='make'\n"
            "decision='allow'\nkind='git_read'\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='make'\ndecision='allow'\nkind='test'\n"
            "surprise = 1\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='../make'\n"
            "decision='allow'\nkind='test'\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='setup.sh'\n"
            "decision='allow'\nkind='test'\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='make'\ndecision='allow'\nkind='test'\n"
            "options=['test']\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='make'\ndecision='allow'\nkind='test'\n"
            "subcommands=['--flag']\n",
            "POLICY_INVALID",
        ),
        (
            "schema_version = 1\n[[command]]\nexecutable='make'\ndecision='allow'\nkind='test'\n"
            "[[command]]\nexecutable='make'\ndecision='allow'\nkind='test'\n",
            "POLICY_DUPLICATE_EXECUTABLE",
        ),
        ("not = valid = toml\n", "POLICY_INVALID"),
    ],
)
def test_an_invalid_policy_fails_whole_rather_than_partly(
    tmp_path: Path, body: str, code: str
) -> None:
    """A policy that partly applied would be an allowance nobody reviewed."""

    _assert_policy_error(tmp_path, body, code)


def test_a_missing_or_non_file_policy_is_reported_distinctly(tmp_path: Path) -> None:
    directory = tmp_path / "policy-directory"
    directory.mkdir()

    with pytest.raises(CommandPolicyFileError) as missing:
        load_project_command_policy(tmp_path / "absent.toml", workspace=tmp_path)
    with pytest.raises(CommandPolicyFileError) as not_a_file:
        load_project_command_policy(directory, workspace=tmp_path)

    assert missing.value.code == "POLICY_NOT_FOUND"
    assert not_a_file.value.code == "POLICY_NOT_A_FILE"


def test_an_oversized_policy_is_refused_without_parsing(tmp_path: Path) -> None:
    _assert_policy_error(tmp_path, "#" + "x" * (64 * 1024), "POLICY_TOO_LARGE")


def test_a_policy_outside_the_workspace_reports_only_its_name(tmp_path: Path) -> None:
    """The trace must not carry the operator's directory layout."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "team-policy.toml"
    outside.write_text(VALID_POLICY, encoding="utf-8")

    policy = load_project_command_policy(outside, workspace=workspace)

    assert policy.source == "team-policy.toml"
    assert str(tmp_path) not in policy.source


def test_rewriting_the_policy_file_cannot_change_a_frozen_decision(tmp_path: Path) -> None:
    """The run holds a snapshot, so a mid-run write decides nothing."""

    policy = _load(tmp_path)
    _write_policy(
        tmp_path,
        """
schema_version = 1

[[command]]
executable = "make"
subcommands = ["install"]
decision = "allow"
kind = "test"
""",
    )

    still_declared = prepare_command(
        tmp_path,
        {"argv": ["make", "test"]},
        environ=_environment(),
        policy=policy,
    )
    with pytest.raises(CommandPolicyError):
        prepare_command(
            tmp_path,
            {"argv": ["make", "install"]},
            environ=_environment(),
            policy=policy,
        )

    assert still_declared.decision is CommandDecision.ALLOW


@pytest.mark.parametrize(
    "relative",
    [POLICY_FILENAME, f"nested/{POLICY_FILENAME}", f"nested/{POLICY_FILENAME.upper()}"],
)
def test_policy_paths_are_recognized_at_any_depth(relative: str) -> None:
    assert is_command_policy_path(relative) is True


@pytest.mark.parametrize("relative", ["pyproject.toml", "proofcoder.toml.bak", "src/policy.toml"])
def test_ordinary_paths_are_not_mistaken_for_a_policy(relative: str) -> None:
    assert is_command_policy_path(relative) is False


def _dispatch(workspace: Path, tool: object, arguments: dict[str, object]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(tool)
    return registry.dispatch(
        ToolCall(
            id="call-1",
            function=FunctionCall(name=tool.name, arguments=json.dumps(arguments)),
        )
    )


@pytest.mark.parametrize("relative", [POLICY_FILENAME, f"nested/{POLICY_FILENAME}"])
def test_no_write_tool_may_author_a_policy_file(tmp_path: Path, relative: str) -> None:
    """A policy the model can write is one a later run would honor."""

    (tmp_path / "nested").mkdir()
    existing = tmp_path / relative
    existing.write_text("schema_version = 1\n", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("keep\n", encoding="utf-8")

    attempts = [
        _dispatch(
            tmp_path,
            create_create_file_tool(tmp_path, checkpoint_available=True),
            {"path": relative, "content": "schema_version = 1\n", "overwrite": True},
        ),
        _dispatch(
            tmp_path,
            create_replace_in_file_tool(tmp_path),
            {"path": relative, "old_text": "schema_version = 1", "new_text": "schema_version = 2"},
        ),
        _dispatch(
            tmp_path,
            create_delete_path_tool(tmp_path, checkpoint_available=True),
            {"path": relative},
        ),
        _dispatch(
            tmp_path,
            create_move_path_tool(tmp_path, checkpoint_available=True),
            {"source": "other.txt", "destination": relative},
        ),
    ]

    assert [attempt.error.code for attempt in attempts if attempt.error] == (
        ["POLICY_PATH_BLOCKED"] * 4
    )
    assert existing.read_text(encoding="utf-8") == "schema_version = 1\n"


def test_reading_a_policy_file_is_still_allowed(tmp_path: Path) -> None:
    """Only writing is refused: a model may legitimately look at project config."""

    _write_policy(tmp_path)

    result = _dispatch(tmp_path, create_read_file_tool(tmp_path), {"path": POLICY_FILENAME})

    assert result.ok is True
    assert result.error is None
