"""Offline tests for deterministic repository compliance evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import proofcoder.compliance as compliance
from proofcoder.compliance import (
    CheckStatus,
    ComplianceInfrastructureError,
    check_capability_evidence,
    check_dependencies,
    format_json,
    format_text,
    normalize_distribution_name,
    run_compliance,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "compliance_check.py"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _dependency_repository(
    root: Path,
    *,
    runtime: tuple[str, ...] = ("openai>=1", "rich>=13"),
    optional: tuple[str, ...] = ("pytest>=8",),
    grouped: tuple[str, ...] = (),
    locked: tuple[str, ...] = ("openai", "rich", "pytest", "proofcoder"),
) -> Path:
    runtime_lines = "\n".join(f'    "{item}",' for item in runtime)
    optional_lines = "\n".join(f'    "{item}",' for item in optional)
    grouped_lines = "\n".join(f'    "{item}",' for item in grouped)
    _write(
        root / "pyproject.toml",
        f"""[project]
name = "proofcoder"
version = "0.0.0"
dependencies = [
{runtime_lines}
]

[project.optional-dependencies]
dev = [
{optional_lines}
]

[dependency-groups]
quality = [
{grouped_lines}
]
""",
    )
    package_blocks = "\n".join(
        f'[[package]]\nname = "{name}"\nversion = "1.0.0"\n' for name in locked
    )
    _write(root / "uv.lock", f"version = 1\nrevision = 1\n\n{package_blocks}")
    return root


def _python_repository(
    root: Path,
    source: str,
    *,
    relative: str = "src/proofcoder/sample.py",
) -> Path:
    _write(root / relative, source)
    return root


def _python_checks(root: Path) -> tuple[compliance.ComplianceCheck, ...]:
    parsed = compliance._parse_python_sources(root)
    return compliance.check_python_sources(parsed)


def _records(
    checks: tuple[compliance.ComplianceCheck, ...],
    check_id: str,
) -> tuple[compliance.ComplianceCheck, ...]:
    return tuple(check for check in checks if check.check_id == check_id)


def test_current_repository_passes_all_automatic_checks() -> None:
    report = run_compliance(PROJECT_ROOT)

    assert report.automatic_pass is True
    assert report.counts["fail"] == 0
    assert report.counts["pass"] > 0


def test_normal_distribution_name_and_plain_openai_are_allowed(tmp_path: Path) -> None:
    root = _dependency_repository(
        tmp_path,
        runtime=("OpenAI>=1.66",),
        optional=("friendly_agent_helpers>=1",),
        locked=("openai", "friendly-agent-helpers"),
    )

    checks = check_dependencies(root)

    assert normalize_distribution_name("My_Package.Name") == "my-package-name"
    assert all(check.status is CheckStatus.PASS for check in checks)


@pytest.mark.parametrize(
    "name",
    [
        "openai-agents",
        "LANGCHAIN_Core",
        "llama.index",
        "Claude_Agent.SDK",
        "PyAutoGen",
        "CrewAI",
        "deepseek_harness",
        "codex-cli",
        "claude.code",
        "OpenCode_SDK",
    ],
)
def test_forbidden_dependency_name_variants_are_rejected(tmp_path: Path, name: str) -> None:
    root = _dependency_repository(tmp_path, runtime=(name,), locked=("proofcoder",))

    failures = [check for check in check_dependencies(root) if check.status is CheckStatus.FAIL]

    assert len(failures) == 1
    assert normalize_distribution_name(name) in failures[0].message
    assert failures[0].path == "pyproject.toml"


def test_optional_and_dependency_group_violations_are_checked(tmp_path: Path) -> None:
    root = _dependency_repository(
        tmp_path,
        optional=("openai_agents>=1",),
        grouped=("langchain.experimental>=1",),
        locked=("proofcoder",),
    )

    failures = [
        check.check_id for check in check_dependencies(root) if check.status is CheckStatus.FAIL
    ]

    assert any(item.startswith("dependency.pyproject.optional.forbidden") for item in failures)
    assert any(item.startswith("dependency.pyproject.groups.forbidden") for item in failures)


def test_uv_lock_transitive_forbidden_package_is_rejected(tmp_path: Path) -> None:
    root = _dependency_repository(
        tmp_path,
        runtime=("openai",),
        locked=("openai", "langchain-community"),
    )

    failures = [check for check in check_dependencies(root) if check.status is CheckStatus.FAIL]

    assert len(failures) == 1
    assert failures[0].check_id.startswith("dependency.uv_lock.forbidden")
    assert failures[0].path == "uv.lock"


def test_invalid_toml_is_an_infrastructure_error(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "not = [valid")
    _write(tmp_path / "uv.lock", "version = 1")

    with pytest.raises(ComplianceInfrastructureError) as raised:
        check_dependencies(tmp_path)

    assert raised.value.code == "TOML_PARSE_ERROR"
    assert str(tmp_path) not in str(raised.value)


@pytest.mark.parametrize(
    ("pyproject", "lock", "code"),
    [
        ("name = 'missing-project'\n", "version = 1\npackage = []\n", "PYPROJECT_STRUCTURE_ERROR"),
        (
            "[project]\nname='x'\nversion='0'\noptional-dependencies=1\n",
            "version = 1\npackage = []\n",
            "PYPROJECT_STRUCTURE_ERROR",
        ),
        (
            "dependency-groups=1\n[project]\nname='x'\nversion='0'\n",
            "version = 1\npackage = []\n",
            "PYPROJECT_STRUCTURE_ERROR",
        ),
        (
            "[project]\nname='x'\nversion='0'\n",
            "version = 1\npackage = 'bad'\n",
            "UV_LOCK_STRUCTURE_ERROR",
        ),
        (
            "[project]\nname='x'\nversion='0'\n",
            "version = 1\n[[package]]\nversion='1'\n",
            "UV_LOCK_STRUCTURE_ERROR",
        ),
    ],
)
def test_invalid_dependency_structures_are_infrastructure_errors(
    tmp_path: Path,
    pyproject: str,
    lock: str,
    code: str,
) -> None:
    _write(tmp_path / "pyproject.toml", pyproject)
    _write(tmp_path / "uv.lock", lock)

    with pytest.raises(ComplianceInfrastructureError) as raised:
        check_dependencies(tmp_path)

    assert raised.value.code == code


def test_dependency_group_include_is_structurally_accepted(tmp_path: Path) -> None:
    _write(
        tmp_path / "pyproject.toml",
        """[project]
name = "proofcoder"
version = "0"

[dependency-groups]
base = ["pytest"]
quality = [{ include-group = "base" }, "ruff"]
""",
    )
    _write(tmp_path / "uv.lock", "version = 1\npackage = []\n")

    checks = check_dependencies(tmp_path)

    assert all(check.status is CheckStatus.PASS for check in checks)


def test_comments_docstrings_and_allowed_imports_do_not_trigger_import_failure(
    tmp_path: Path,
) -> None:
    root = _python_repository(
        tmp_path,
        '''"""langchain and from agents import Agent are documentation only."""
# import crewai
import json
from proofcoder import compliance
''',
    )

    checks = _python_checks(root)

    assert not _records(checks, "python.import.forbidden")
    assert _records(checks, "python.imports")[0].status is CheckStatus.PASS


@pytest.mark.parametrize(
    "source,module",
    [
        ("import langchain.agents\n", "langchain.agents"),
        ("from llama_index.core import VectorStoreIndex\n", "llama_index.core"),
        ("from agents import Agent\n", "agents"),
        ("import claude_agent_sdk\n", "claude_agent_sdk"),
    ],
)
def test_forbidden_import_and_from_import_are_reported(
    tmp_path: Path,
    source: str,
    module: str,
) -> None:
    checks = _python_checks(_python_repository(tmp_path, source))

    failure = _records(checks, "python.import.forbidden")[0]
    assert failure.status is CheckStatus.FAIL
    assert module in failure.message
    assert failure.path == "src/proofcoder/sample.py"
    assert failure.line == 1


@pytest.mark.parametrize(
    "source",
    [
        'import subprocess\nsubprocess.run(["codex", "exec", "task"])\n',
        'from subprocess import Popen as start\nstart(["claude", "--print"])\n',
        'import os\nos.system("opencode run task")\n',
        'import subprocess\nsubprocess.check_call(["npx", "deepseek-harness"])\n',
    ],
)
def test_explicit_agent_subprocesses_fail(tmp_path: Path, source: str) -> None:
    checks = _python_checks(_python_repository(tmp_path, source))

    failure = _records(checks, "python.subprocess.forbidden_agent")[0]
    assert failure.status is CheckStatus.FAIL
    assert failure.path == "src/proofcoder/sample.py"
    assert failure.line == 2


def test_allowed_git_python_pytest_and_ruff_subprocesses_pass(tmp_path: Path) -> None:
    source = """import subprocess
subprocess.run(["git", "status"], shell=False)
subprocess.Popen(["python", "-m", "pytest", "-q"], shell=False)
subprocess.call(["pytest", "tests"], shell=False)
subprocess.check_output(["ruff", "check", "."], shell=False)
"""

    checks = _python_checks(_python_repository(tmp_path, source))

    assert not _records(checks, "python.subprocess.forbidden_agent")
    assert not _records(checks, "python.subprocess.dynamic")
    assert _records(checks, "python.subprocesses")[0].status is CheckStatus.PASS


def test_dynamic_subprocess_is_manual_review_not_failure(tmp_path: Path) -> None:
    source = "import subprocess\ncommand = choose_command()\nsubprocess.run(command)\n"

    checks = _python_checks(_python_repository(tmp_path, source))

    review = _records(checks, "python.subprocess.dynamic")[0]
    assert review.status is CheckStatus.REVIEW
    assert review.line == 3
    assert not any(check.status is CheckStatus.FAIL for check in checks)


def test_hosted_tool_schema_files_api_and_file_id_fail(tmp_path: Path) -> None:
    source = """def request(client):
    tools = [{"type": "code_interpreter"}, {"type": "file_search"}]
    client.files.create(file=b"fake")
    client.responses.create(file_id="file_fake", tools=tools)
"""

    checks = _python_checks(_python_repository(tmp_path, source))
    failure_ids = {check.check_id for check in checks if check.status is CheckStatus.FAIL}

    assert "python.hosted_tool.schema" in failure_ids
    assert "python.hosted_tool.files_api" in failure_ids
    assert "python.hosted_tool.file_id" in failure_ids


def test_local_function_tool_schema_and_chat_completions_are_allowed(tmp_path: Path) -> None:
    source = """def request(client):
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    return client.chat.completions.create(messages=[], tools=tools)
"""

    checks = _python_checks(_python_repository(tmp_path, source))

    assert not any(check.status is CheckStatus.FAIL for check in checks)
    assert not _records(checks, "python.hosted_tool.dynamic_request")


def test_dynamic_provider_request_is_manual_review(tmp_path: Path) -> None:
    source = "def request(client, payload):\n    return client.responses.create(**payload)\n"

    checks = _python_checks(_python_repository(tmp_path, source))

    review = _records(checks, "python.hosted_tool.dynamic_request")[0]
    assert review.status is CheckStatus.REVIEW
    assert review.line == 2


def test_missing_implementation_and_test_files_fail_capability_mapping(tmp_path: Path) -> None:
    checks = check_capability_evidence(tmp_path)

    deepseek = _records(checks, "capability.deepseek_client")[0]
    assert deepseek.status is CheckStatus.FAIL
    assert "invalid implementation file" in deepseek.message
    assert "missing regular test file" in deepseek.message


def test_missing_key_symbol_fails_capability_mapping(tmp_path: Path) -> None:
    _write(tmp_path / "src/proofcoder/llm/deepseek.py", "class OtherClient:\n    pass\n")
    _write(tmp_path / "tests/unit/test_deepseek.py", "def test_other():\n    pass\n")

    checks = check_capability_evidence(tmp_path)

    deepseek = _records(checks, "capability.deepseek_client")[0]
    assert deepseek.status is CheckStatus.FAIL
    assert "missing symbols" in deepseek.message
    assert "DeepSeekClient" in deepseek.message


def test_symlinked_capability_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "outside.py"
    _write(target, "class DeepSeekClient:\n    pass\n")
    link = tmp_path / "src/proofcoder/llm/deepseek.py"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    _write(tmp_path / "tests/unit/test_deepseek.py", "def test_client():\n    pass\n")

    deepseek = _records(check_capability_evidence(tmp_path), "capability.deepseek_client")[0]

    assert deepseek.status is CheckStatus.FAIL
    assert "invalid implementation file" in deepseek.message


def test_python_symlink_and_syntax_error_are_infrastructure_errors(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    _write(source, "value = 1\n")
    link = tmp_path / "src/proofcoder/link.py"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ComplianceInfrastructureError) as symlink_error:
        compliance._parse_python_sources(tmp_path)
    assert symlink_error.value.code == "SYMLINK_REJECTED"

    link.unlink()
    _write(tmp_path / "src/proofcoder/bad.py", "def broken(:\n")
    with pytest.raises(ComplianceInfrastructureError) as syntax_error:
        compliance._parse_python_sources(tmp_path)
    assert syntax_error.value.code == "PYTHON_PARSE_ERROR"
    assert "bad.py:1" in str(syntax_error.value)


def test_unreadable_and_oversized_python_files_are_stable_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "src/proofcoder/bad.py"
    _write(bad, "value = 1\n")
    original_read_bytes = Path.read_bytes

    def fail_selected(path: Path) -> bytes:
        if path.name == "bad.py":
            raise OSError("private operating system detail")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected)
    with pytest.raises(ComplianceInfrastructureError) as unreadable:
        compliance._parse_python_sources(tmp_path)
    assert unreadable.value.code == "FILE_UNREADABLE"
    assert "private operating system detail" not in str(unreadable.value)

    monkeypatch.undo()
    bad.write_bytes(b"x" * (compliance.MAX_CHECKED_FILE_BYTES + 1))
    with pytest.raises(ComplianceInfrastructureError) as oversized:
        compliance._parse_python_sources(tmp_path)
    assert oversized.value.code == "FILE_TOO_LARGE"


def test_json_is_deterministic_parseable_relative_and_has_manual_review() -> None:
    first = format_json(run_compliance(PROJECT_ROOT))
    second = format_json(run_compliance(PROJECT_ROOT))
    decoded = json.loads(first)

    assert first == second
    assert decoded["schema_version"] == 1
    assert decoded["automatic_pass"] is True
    assert isinstance(decoded["manual_review"], list)
    assert decoded["manual_review"]
    assert str(PROJECT_ROOT) not in first
    assert "\\" not in "".join(str(check.get("path", "")) for check in decoded["checks"])


def test_text_output_has_ordered_statuses_summary_and_boundary_notice() -> None:
    report = run_compliance(PROJECT_ROOT)

    text = format_text(report)

    assert "PASS capability.agent_loop" in text
    assert "REVIEW python." in text
    assert "Summary: PASS=" in text
    assert "Automatic checks: PASS" in text
    assert compliance.DISCLAIMER in text


def test_results_never_include_source_body_environment_or_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "FAKE_PRIVATE_SENTINEL_8421"
    monkeypatch.setenv("FAKE_SECRET_TOKEN", sentinel)
    root = _dependency_repository(tmp_path)
    _python_repository(
        root,
        f"# {sentinel}\nimport subprocess\ncommand = unknown()\nsubprocess.run(command)\n",
    )

    rendered = format_json(
        compliance.ComplianceReport(
            tuple(sorted(_python_checks(root), key=compliance._check_sort_key))
        )
    )

    assert sentinel not in rendered
    assert "FAKE_SECRET_TOKEN" not in rendered
    assert "traceback" not in rendered.casefold()
    assert "subprocess.run(command)" not in rendered


def test_checker_does_not_read_env_runtime_or_virtual_environment(tmp_path: Path) -> None:
    root = _dependency_repository(tmp_path)
    _python_repository(root, "value = 1\n")
    (root / ".env").write_bytes(b"\xff\x00fake")
    _write(root / ".proofcoder/runtime/bad.py", "def broken(:\n")
    _write(root / ".venv/bad.py", "def broken(:\n")

    report = run_compliance(root)

    assert isinstance(report, compliance.ComplianceReport)
    assert any(check.check_id.startswith("capability.") for check in report.checks)


def test_script_text_and_json_exit_codes(tmp_path: Path) -> None:
    passing = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(PROJECT_ROOT)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    failing_root = _dependency_repository(tmp_path / "failing", runtime=("openai-agents",))
    failing = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(failing_root), "--format", "json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    invalid = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path / "missing")],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert passing.returncode == 0
    assert "Automatic checks: PASS" in passing.stdout
    assert failing.returncode == 1
    assert json.loads(failing.stdout)["automatic_pass"] is False
    assert failing.stderr == ""
    assert invalid.returncode == 2
    assert "ERROR ROOT_INVALID" in invalid.stdout
    assert "Traceback" not in invalid.stdout + invalid.stderr


def test_json_infrastructure_error_is_safe_and_parseable(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path / "missing"),
            "--format",
            "json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env={**os.environ, "FAKE_SECRET_TOKEN": "FAKE_SECRET_VALUE"},
    )
    decoded = json.loads(completed.stdout)

    assert completed.returncode == 2
    assert decoded["schema_version"] == 1
    assert decoded["automatic_pass"] is False
    assert decoded["infrastructure_error"]["code"] == "ROOT_INVALID"
    assert "FAKE_SECRET_VALUE" not in completed.stdout


def test_direct_infrastructure_error_json_has_fixed_safe_schema() -> None:
    rendered = compliance.infrastructure_error_json(
        ComplianceInfrastructureError("FAKE_ERROR", "safe bounded message")
    )
    decoded = json.loads(rendered)

    assert decoded["schema_version"] == 1
    assert decoded["automatic_pass"] is False
    assert decoded["checks"] == []
    assert decoded["manual_review"] == []
    assert decoded["infrastructure_error"] == {
        "code": "FAKE_ERROR",
        "message": "safe bounded message",
    }
