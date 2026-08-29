"""Offline evaluation fixture loading, materialization, and rejection tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from proofcoder.eval_fixtures import (
    EvalFixtureError,
    FixtureCategory,
    load_fixtures,
    materialize_fixture,
)
from proofcoder.safety.secrets import minimal_subprocess_environment

FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "evals" / "fixtures"
EXPECTED_FIXTURES = {
    "bugfix-inclusive-total": FixtureCategory.BUG_FIX,
    "cross-file-message-format": FixtureCategory.CROSS_FILE_CHANGE,
    "feature-available-items": FixtureCategory.FEATURE_ADDITION,
}
FORBIDDEN_TASK_HINTS = {
    "create_file",
    "finish_task",
    "list_files",
    "read_file",
    "replace_in_file",
    "run_command",
    "search_text",
}


def _metadata(fixture_id: str = "sample-fixture") -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": fixture_id,
        "category": "bug_fix",
        "task": "Correct the sample behavior and keep its tests passing.",
        "validation": {
            "argv": ["python", "-m", "unittest", "-q"],
            "cwd": ".",
            "success_exit_code": 0,
            "initial_exit_code": 1,
            "initial_output_contains": "expected failure",
        },
        "allowed_modified_files": ["sample.py"],
        "required_modified_files": ["sample.py"],
    }


def _write_fixture(
    root: Path,
    directory_name: str,
    *,
    metadata: dict[str, object] | None = None,
) -> Path:
    directory = root / directory_name
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "sample.py").write_text("VALUE = 'broken'\n", encoding="utf-8")
    value = _metadata(directory_name) if metadata is None else metadata
    (directory / "fixture.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return directory


def _assert_code(root: Path, code: str) -> None:
    with pytest.raises(EvalFixtureError) as captured:
        load_fixtures(root)
    assert captured.value.code == code


def test_repository_fixtures_load_in_deterministic_complete_order() -> None:
    fixtures = load_fixtures(FIXTURES_ROOT)

    assert [fixture.fixture_id for fixture in fixtures] == sorted(EXPECTED_FIXTURES)
    assert {fixture.fixture_id: fixture.category for fixture in fixtures} == EXPECTED_FIXTURES
    assert all(fixture.task.strip() == fixture.task for fixture in fixtures)
    assert all(fixture.validation.argv[0] == "python" for fixture in fixtures)
    assert all(fixture.validation.cwd == "." for fixture in fixtures)
    assert all(fixture.validation.success_exit_code == 0 for fixture in fixtures)
    assert all(fixture.validation.initial_exit_code != 0 for fixture in fixtures)
    assert all(
        set(fixture.required_modified_files) <= set(fixture.allowed_modified_files)
        for fixture in fixtures
    )
    assert all(
        not any(hint in fixture.task.casefold() for hint in FORBIDDEN_TASK_HINTS)
        for fixture in fixtures
    )


@pytest.mark.parametrize("fixture_id", sorted(EXPECTED_FIXTURES))
def test_materialization_copies_only_workspace_files(tmp_path: Path, fixture_id: str) -> None:
    fixture = next(item for item in load_fixtures(FIXTURES_ROOT) if item.fixture_id == fixture_id)
    destination = tmp_path / "materialized"

    copied = materialize_fixture(fixture, destination)

    actual = tuple(
        sorted(
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        )
    )
    assert copied == fixture.workspace_files
    assert actual == fixture.workspace_files
    assert not (destination / "fixture.json").exists()
    assert "initial_output_contains" not in "\n".join(
        path.read_text(encoding="utf-8") for path in destination.rglob("*") if path.is_file()
    )


@pytest.mark.parametrize("fixture_id", sorted(EXPECTED_FIXTURES))
def test_each_initial_validation_fails_for_the_declared_reason(
    tmp_path: Path, fixture_id: str
) -> None:
    fixture = next(item for item in load_fixtures(FIXTURES_ROOT) if item.fixture_id == fixture_id)
    destination = tmp_path / fixture_id
    materialize_fixture(fixture, destination)
    environment = minimal_subprocess_environment(command_defaults=True)
    environment["PATH"] = str(Path(os.sys.executable).resolve().parent)

    completed = subprocess.run(
        fixture.validation.argv,
        cwd=destination / fixture.validation.cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        shell=False,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode == fixture.validation.initial_exit_code
    assert fixture.validation.initial_output_contains in output


@pytest.mark.parametrize("unsafe_path", ["../outside.py", "/outside.py", "C:/outside.py"])
def test_absolute_and_traversal_metadata_paths_are_rejected(
    tmp_path: Path, unsafe_path: str
) -> None:
    metadata = _metadata()
    metadata["allowed_modified_files"] = [unsafe_path]
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "UNSAFE_FIXTURE_PATH")


def test_traversal_validation_cwd_is_rejected(tmp_path: Path) -> None:
    metadata = _metadata()
    validation = dict(metadata["validation"])  # type: ignore[arg-type]
    validation["cwd"] = "../outside"
    metadata["validation"] = validation
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "UNSAFE_FIXTURE_PATH")


def test_empty_and_non_directory_roots_are_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _assert_code(empty, "FIXTURE_ROOT_INVALID")

    not_directory = tmp_path / "fixtures.json"
    not_directory.write_text("{}\n", encoding="utf-8")
    _assert_code(not_directory, "FIXTURE_ROOT_INVALID")


def test_root_files_and_unexpected_fixture_entries_are_rejected(tmp_path: Path) -> None:
    root_file = tmp_path / "root-file"
    root_file.mkdir()
    (root_file / "unexpected.json").write_text("{}\n", encoding="utf-8")
    _assert_code(root_file, "FIXTURE_LAYOUT_INVALID")

    bad_layout = tmp_path / "bad-layout"
    directory = _write_fixture(bad_layout, "fixture")
    (directory / "answer.txt").write_text("not permitted\n", encoding="utf-8")
    _assert_code(bad_layout, "FIXTURE_LAYOUT_INVALID")


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "INVALID"), ("category", "unsupported"), ("task", "")],
)
def test_invalid_identity_category_and_task_are_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    metadata = _metadata()
    metadata[field] = value
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    directory = _write_fixture(tmp_path, "fixture")
    (directory / "fixture.json").write_bytes(b"{broken}\n")

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["schema_version"] = 2
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("argv", []),
        ("argv", [" python"]),
        ("success_exit_code", 1),
        ("success_exit_code", False),
        ("initial_exit_code", 0),
        ("initial_exit_code", "1"),
        ("initial_output_contains", ""),
    ],
)
def test_invalid_validation_contract_is_rejected(tmp_path: Path, field: str, value: object) -> None:
    metadata = _metadata()
    validation = dict(metadata["validation"])  # type: ignore[arg-type]
    validation[field] = value
    metadata["validation"] = validation
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


def test_legacy_validation_fields_are_rejected(tmp_path: Path) -> None:
    metadata = _metadata()
    validation = dict(metadata["validation"])  # type: ignore[arg-type]
    validation["expected_exit_code"] = validation.pop("initial_exit_code")
    validation["expected_output_contains"] = validation.pop("initial_output_contains")
    metadata["validation"] = validation
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


@pytest.mark.parametrize("paths", [[], ["nested\\sample.py"]])
def test_empty_and_noncanonical_path_lists_are_rejected(tmp_path: Path, paths: list[str]) -> None:
    metadata = _metadata()
    metadata["allowed_modified_files"] = paths
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    expected = "FIXTURE_METADATA_INVALID" if not paths else "UNSAFE_FIXTURE_PATH"
    _assert_code(tmp_path, expected)


def test_empty_workspace_and_missing_validation_cwd_are_rejected(tmp_path: Path) -> None:
    empty_root = tmp_path / "empty-workspace"
    empty = _write_fixture(empty_root, "fixture")
    (empty / "workspace" / "sample.py").unlink()
    _assert_code(empty_root, "FIXTURE_LAYOUT_INVALID")

    cwd_root = tmp_path / "missing-cwd"
    metadata = _metadata()
    validation = dict(metadata["validation"])  # type: ignore[arg-type]
    validation["cwd"] = "missing"
    metadata["validation"] = validation
    _write_fixture(cwd_root, "fixture", metadata=metadata)
    _assert_code(cwd_root, "FIXTURE_METADATA_INVALID")


def test_workspace_symlink_is_rejected(tmp_path: Path) -> None:
    directory = _write_fixture(tmp_path / "fixtures", "fixture")
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        (directory / "workspace" / "linked.py").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {error}")

    _assert_code(tmp_path / "fixtures", "FIXTURE_SYMLINK")


def test_sensitive_workspace_file_is_rejected(tmp_path: Path) -> None:
    directory = _write_fixture(tmp_path, "fixture")
    (directory / "workspace" / ".env").write_text("not-a-secret\n", encoding="utf-8")

    _assert_code(tmp_path, "SENSITIVE_FIXTURE_PATH")


def test_sensitive_metadata_path_is_rejected(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["allowed_modified_files"] = ["credentials.json"]
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "SENSITIVE_FIXTURE_PATH")


def test_duplicate_fixture_ids_are_rejected(tmp_path: Path) -> None:
    duplicate = _metadata("duplicate-id")
    _write_fixture(tmp_path, "first", metadata=duplicate)
    _write_fixture(tmp_path, "second", metadata=duplicate)

    _assert_code(tmp_path, "DUPLICATE_FIXTURE_ID")


def test_duplicate_metadata_paths_are_rejected_case_insensitively(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["allowed_modified_files"] = ["sample.py", "SAMPLE.py"]
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "DUPLICATE_FIXTURE_PATH")


def test_materialization_rejects_workspace_changes_after_loading(tmp_path: Path) -> None:
    copied_root = tmp_path / "fixtures"
    directory = _write_fixture(copied_root, "fixture")
    loaded = load_fixtures(copied_root)[0]
    (directory / "workspace" / "later.py").write_text("LATER = True\n", encoding="utf-8")

    with pytest.raises(EvalFixtureError) as captured:
        materialize_fixture(loaded, tmp_path / "target")

    assert captured.value.code == "FIXTURE_CHANGED"


def test_required_paths_must_be_allowed(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["required_modified_files"] = ["other.py"]
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


def test_unknown_metadata_fields_are_rejected(tmp_path: Path) -> None:
    metadata = _metadata()
    metadata["solution"] = "not permitted"
    _write_fixture(tmp_path, "fixture", metadata=metadata)

    _assert_code(tmp_path, "FIXTURE_METADATA_INVALID")


@pytest.mark.parametrize("target_kind", ["file", "nonempty_directory"])
def test_materialization_never_overwrites_target(tmp_path: Path, target_kind: str) -> None:
    fixture = load_fixtures(FIXTURES_ROOT)[0]
    target = tmp_path / "target"
    if target_kind == "file":
        target.write_text("keep\n", encoding="utf-8")
    else:
        target.mkdir()
        (target / "keep.txt").write_text("keep\n", encoding="utf-8")

    with pytest.raises(EvalFixtureError) as captured:
        materialize_fixture(fixture, target)

    assert captured.value.code == "TARGET_NOT_EMPTY"
    preserved = target if target.is_file() else target / "keep.txt"
    assert preserved.read_text(encoding="utf-8") == "keep\n"


def test_materialization_rejects_symlink_target(tmp_path: Path) -> None:
    fixture = load_fixtures(FIXTURES_ROOT)[0]
    actual = tmp_path / "actual"
    actual.mkdir()
    target = tmp_path / "target"
    try:
        target.symlink_to(actual, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(EvalFixtureError) as captured:
        materialize_fixture(fixture, target)

    assert captured.value.code == "TARGET_UNSAFE"
    assert not tuple(actual.iterdir())


def test_existing_empty_target_is_accepted(tmp_path: Path) -> None:
    fixture = load_fixtures(FIXTURES_ROOT)[0]
    target = tmp_path / "target"
    target.mkdir()

    materialize_fixture(fixture, target)

    assert tuple(path for path in target.rglob("*") if path.is_file())
