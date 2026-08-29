"""Offline Git-surface, detection, limit, determinism, and CLI tests."""

from __future__ import annotations

import codecs
import importlib.util
import io
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import proofcoder.secret_scan as secret_scan
from proofcoder.secret_scan import (
    DEFAULT_SCOPES,
    ScanLimits,
    ScanScope,
    format_json,
    format_text,
    scan_repository,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "secret_scan.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("proofcoder_secret_scan_script", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
secret_scan_script = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(secret_scan_script)


def _git_environment() -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    environment = {name: os.environ[name] for name in os.environ if name.upper() in allowed}
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def _initialize_repository(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Secret Scan Tests"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.email", "secret-scan@example.invalid"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )
    return root


def _commit_all(root: Path) -> None:
    subprocess.run(
        ["git", "add", "."],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test state"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )


def _force_add_environment_file(root: Path) -> None:
    subprocess.run(
        ["git", "add", "-f", ".env"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )


def _create_topic_branch(root: Path) -> None:
    subprocess.run(
        ["git", "switch", "-q", "-c", "topic"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )


def _switch_main(root: Path) -> None:
    subprocess.run(
        ["git", "switch", "-q", "main"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )


def _tag_topic(root: Path) -> None:
    subprocess.run(
        ["git", "tag", "topic-evidence"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )


def _delete_topic_branch(root: Path) -> None:
    subprocess.run(
        ["git", "branch", "-D", "topic"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
        stdout=subprocess.DEVNULL,
    )


def _repository_status(root: Path) -> bytes:
    return subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
    ).stdout


def _repository_head(root: Path) -> bytes:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
        stdout=subprocess.PIPE,
    ).stdout


def _runtime_key() -> str:
    return "sk" + "-" + "A" * 24


def _assignment(secret: str) -> str:
    return "API" + "_KEY=" + secret + "\n"


def _clean_repository(root: Path) -> Path:
    _initialize_repository(root)
    (root / "safe.txt").write_text("ordinary content\n", encoding="utf-8")
    _commit_all(root)
    return root


def _rules(report: secret_scan.SecretScanReport, scope: ScanScope) -> set[str]:
    return {finding.rule_id for finding in report.findings if finding.scope is scope}


def test_current_repository_all_scopes_pass_without_changing_git_state() -> None:
    before_status = _repository_status(PROJECT_ROOT)
    before_head = _repository_head(PROJECT_ROOT)

    first = scan_repository(PROJECT_ROOT)
    second = scan_repository(PROJECT_ROOT)

    assert first.scan_complete is True
    assert first.automatic_pass is True
    assert first.exit_code == 0
    assert format_json(first) == format_json(second)
    assert set(first.scopes) == set(DEFAULT_SCOPES)
    assert _repository_status(PROJECT_ROOT) == before_status
    assert _repository_head(PROJECT_ROOT) == before_head


def test_clean_temporary_repository_all_scopes_pass(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")

    report = scan_repository(root)

    assert report.scan_complete is True
    assert report.automatic_pass is True
    assert report.findings == ()
    assert all(statistics.scanned > 0 for statistics in report.scopes.values())


def test_untracked_working_tree_secret_is_isolated_from_index_and_history(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    value = _runtime_key()
    (root / "new file 中文.txt").write_text(value, encoding="utf-8")

    report = scan_repository(root)

    assert "content.openai_api_key" in _rules(report, ScanScope.WORKING_TREE)
    assert _rules(report, ScanScope.INDEX) == set()
    assert _rules(report, ScanScope.HISTORY) == set()


def test_staged_secret_is_scanned_after_working_tree_is_cleaned(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    target = root / "safe.txt"
    target.write_text(_runtime_key(), encoding="utf-8")
    subprocess.run(
        ["git", "add", "."],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )
    target.write_text("clean again\n", encoding="utf-8")

    report = scan_repository(root)

    assert _rules(report, ScanScope.WORKING_TREE) == set()
    assert "content.openai_api_key" in _rules(report, ScanScope.INDEX)
    assert _rules(report, ScanScope.HISTORY) == set()


def test_working_tree_secret_does_not_replace_clean_staged_blob(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    (root / "safe.txt").write_text(_runtime_key(), encoding="utf-8")

    report = scan_repository(root)

    assert "content.openai_api_key" in _rules(report, ScanScope.WORKING_TREE)
    assert _rules(report, ScanScope.INDEX) == set()
    assert _rules(report, ScanScope.HISTORY) == set()


def test_deleted_current_secret_remains_detectable_in_history(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    target = root / "historical.txt"
    target.write_text(_runtime_key(), encoding="utf-8")
    _commit_all(root)
    target.write_text("clean current value\n", encoding="utf-8")
    _commit_all(root)

    report = scan_repository(root)

    assert _rules(report, ScanScope.WORKING_TREE) == set()
    assert _rules(report, ScanScope.INDEX) == set()
    assert "content.openai_api_key" in _rules(report, ScanScope.HISTORY)


def test_history_scans_noncurrent_branch(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    _create_topic_branch(root)
    (root / "branch-only.txt").write_text(_runtime_key(), encoding="utf-8")
    _commit_all(root)
    _switch_main(root)

    report = scan_repository(root, scopes=(ScanScope.HISTORY,))

    assert "content.openai_api_key" in _rules(report, ScanScope.HISTORY)


def test_history_scans_tag_after_branch_deletion(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    _create_topic_branch(root)
    (root / "tag-only.txt").write_text(_runtime_key(), encoding="utf-8")
    _commit_all(root)
    _tag_topic(root)
    _switch_main(root)
    _delete_topic_branch(root)

    report = scan_repository(root, scopes=(ScanScope.HISTORY,))

    assert "content.openai_api_key" in _rules(report, ScanScope.HISTORY)


def test_sensitive_environment_path_is_reported_without_opening_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    environment_path = root / ".env"
    environment_path.write_text(_runtime_key(), encoding="utf-8")
    _force_add_environment_file(root)
    subprocess.run(
        ["git", "commit", "-q", "-m", "tracked sensitive path"],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path.name.casefold() == ".env":
            raise AssertionError("sensitive local file content must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    report = scan_repository(root)

    assert report.scan_complete is True
    assert {finding.scope for finding in report.findings} == set(DEFAULT_SCOPES)
    assert {finding.rule_id for finding in report.findings} == {"path.sensitive"}


def test_ignored_untracked_environment_file_is_never_opened_or_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _initialize_repository(tmp_path / "repository")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    (root / "safe.txt").write_text("safe\n", encoding="utf-8")
    _commit_all(root)
    (root / ".env").write_text(_runtime_key(), encoding="utf-8")
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path.name.casefold() == ".env":
            raise AssertionError("ignored environment file must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    report = scan_repository(root)
    rendered = format_json(report) + format_text(report)

    assert report.automatic_pass is True
    assert ".env" not in rendered


def test_environment_template_placeholders_are_allowed(tmp_path: Path) -> None:
    root = _initialize_repository(tmp_path / "repository")
    values = (
        "",
        "${SERVICE_KEY}",
        "$env:SERVICE_KEY",
        "%SERVICE_KEY%",
        "<your-key>",
        "your-api-key",
        "changeme",
        "obviously-fake-value",
    )
    content = "\n".join(_assignment(value).rstrip("\n") for value in values) + "\n"
    (root / ".env.example").write_text(content, encoding="utf-8")
    _commit_all(root)

    report = scan_repository(root)

    assert report.automatic_pass is True


def test_content_rule_categories_and_sensitive_assignment_substring(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    header = "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" + "-" * 5
    github = "ghp" + "_" + "G" * 24
    aws = "AKIA" + "A" * 16
    google = "AIza" + "A" * 35
    slack = "xoxb" + "-" + "A" * 16
    bearer = "Authorization: Bearer " + "B" * 20
    basic = "Authorization: Basic " + "QWxh" + "ZGRpbjpvcGVuIHNlc2FtZQ=="
    assignment = _assignment("prefix-test-middle-real-value")
    credential_url = "https://" + "alice:" + "runtime-password" + "@example.invalid"
    (root / "rules.txt").write_text(
        "\n".join(
            (
                header,
                _runtime_key(),
                github,
                aws,
                google,
                slack,
                bearer,
                basic,
                assignment,
                credential_url,
            )
        ),
        encoding="utf-8",
    )

    report = scan_repository(root, scopes=(ScanScope.WORKING_TREE,))

    assert {
        "content.private_key",
        "content.openai_api_key",
        "content.github_token",
        "content.aws_access_key_id",
        "content.google_api_key",
        "content.slack_token",
        "content.authorization_credential",
        "content.sensitive_assignment",
        "content.credential_url",
    } <= _rules(report, ScanScope.WORKING_TREE)


def test_field_names_and_plain_urls_do_not_trigger(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    (root / "documentation.txt").write_text(
        "DEEPSEEK_API_KEY Authorization reasoning_content\nhttps://example.invalid/path\n",
        encoding="utf-8",
    )

    report = scan_repository(root, scopes=(ScanScope.WORKING_TREE,))

    assert report.automatic_pass is True


def test_binary_bom_decode_and_safe_output_redaction(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    value = _runtime_key()
    (root / "binary.bin").write_bytes(b"plain\0" + value.encode("ascii"))
    (root / "bom.txt").write_bytes(codecs.BOM_UTF8 + value.encode("ascii"))

    report = scan_repository(root, scopes=(ScanScope.WORKING_TREE,))
    rendered_json = format_json(report)
    rendered_text = format_text(report)

    statistics = report.scopes[ScanScope.WORKING_TREE]
    assert statistics.skipped_binary == 1
    assert statistics.scanned == 2
    assert "content.openai_api_key" in _rules(report, ScanScope.WORKING_TREE)
    assert value not in rendered_json + rendered_text
    assert str(root) not in rendered_json + rendered_text
    assert "Traceback" not in rendered_json + rendered_text


@pytest.mark.parametrize(
    ("raw", "limits", "code"),
    [
        (b"legacy-\xff-text", ScanLimits(), "TEXT_DECODE_ERROR"),
        (b"x" * 17, ScanLimits(max_content_bytes=16), "CONTENT_LIMIT_EXCEEDED"),
    ],
)
def test_non_utf8_and_oversized_text_are_incomplete(
    tmp_path: Path,
    raw: bytes,
    limits: ScanLimits,
    code: str,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    (root / "problem.txt").write_bytes(raw)

    report = scan_repository(root, scopes=(ScanScope.WORKING_TREE,), limits=limits)

    assert report.scan_complete is False
    assert report.automatic_pass is False
    assert report.exit_code == 2
    assert code in {error.code for error in report.errors}


def test_candidate_and_finding_limits_cannot_return_success(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    (root / "first.txt").write_text(_runtime_key(), encoding="utf-8")
    (root / "second.txt").write_text(_runtime_key(), encoding="utf-8")

    candidate_limited = scan_repository(
        root,
        scopes=(ScanScope.WORKING_TREE,),
        limits=ScanLimits(max_candidates=1),
    )
    finding_limited = scan_repository(
        root,
        scopes=(ScanScope.WORKING_TREE,),
        limits=ScanLimits(max_findings=1),
    )

    assert candidate_limited.exit_code == 2
    assert finding_limited.exit_code == 2
    assert len(finding_limited.findings) == 1
    assert "FINDING_LIMIT_EXCEEDED" in {error.code for error in finding_limited.errors}


def test_working_tree_symlink_is_not_followed(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    outside = tmp_path / "outside.txt"
    outside.write_text(_runtime_key(), encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    report = scan_repository(root, scopes=(ScanScope.WORKING_TREE,))

    assert report.exit_code == 2
    assert "SYMLINK_REJECTED" in {error.code for error in report.errors}
    assert report.findings == ()


def test_findings_are_sorted_and_same_blob_is_read_once(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    value = _runtime_key()
    (root / "z 中文.txt").write_text(value, encoding="utf-8")
    (root / "a space.txt").write_text(value, encoding="utf-8")
    _commit_all(root)

    report = scan_repository(root)
    paths = [finding.path for finding in report.findings]

    assert paths[:2] == ["a space.txt", "z 中文.txt"]
    assert report.scopes[ScanScope.INDEX].scanned == 2
    assert report.scopes[ScanScope.HISTORY].scanned == 2
    assert all(
        finding.blob_oid_prefix
        for finding in report.findings
        if finding.scope is not ScanScope.WORKING_TREE
    )


def test_process_secret_values_are_not_read_or_scanned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _clean_repository(tmp_path / "repository")

    class GuardedEnvironment(dict[str, str]):
        def __getitem__(self, name: str) -> str:
            if name == "API_TOKEN":
                raise AssertionError("secret environment value must not be read")
            return super().__getitem__(name)

    environment = GuardedEnvironment(
        {
            "PATH": os.environ.get("PATH", ""),
            "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "API_TOKEN": _runtime_key(),
        }
    )
    monkeypatch.setattr(secret_scan.os, "environ", environment)

    report = scan_repository(root)

    assert report.automatic_pass is True


def test_non_repository_git_unavailable_and_git_failure_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_repository = tmp_path / "ordinary"
    non_repository.mkdir()
    invalid = scan_repository(non_repository)
    assert invalid.exit_code == 2
    assert invalid.errors[0].code == "NOT_A_GIT_REPOSITORY"

    monkeypatch.setattr(
        secret_scan.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    unavailable = scan_repository(non_repository)
    assert unavailable.errors[0].code == "GIT_NOT_AVAILABLE"

    sentinel = _runtime_key()
    monkeypatch.setattr(
        secret_scan.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=sentinel.encode("ascii"),
        ),
    )
    failed = scan_repository(non_repository)
    assert failed.exit_code == 2
    assert sentinel not in format_json(failed) + format_text(failed)


def test_json_schema_text_summary_and_cli_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clean = _clean_repository(tmp_path / "clean")
    code = secret_scan_script.main(
        ["--root", str(clean), "--scope", "working-tree", "--format", "json"]
    )
    clean_output = capsys.readouterr()
    decoded = json.loads(clean_output.out)
    assert code == 0
    assert decoded["schema_version"] == 1
    assert decoded["scan_complete"] is True
    assert decoded["automatic_pass"] is True
    assert set(decoded["scopes"]) == {"working_tree"}

    (clean / "unsafe.txt").write_text(_runtime_key(), encoding="utf-8")
    assert secret_scan_script.main(["--root", str(clean), "--scope", "working-tree"]) == 1
    finding_output = capsys.readouterr()
    assert "scan complete: true" in finding_output.out
    assert "automatic pass: false" in finding_output.out

    invalid = tmp_path / "not-git"
    invalid.mkdir()
    assert secret_scan_script.main(["--root", str(invalid), "--format", "json"]) == 2
    error_output = capsys.readouterr()
    assert json.loads(error_output.out)["scan_complete"] is False
    assert error_output.err == ""


@pytest.mark.parametrize(
    ("target", "scope"),
    [
        ("working", ScanScope.WORKING_TREE),
        ("index", ScanScope.INDEX),
        ("objects", ScanScope.HISTORY),
        ("changes", ScanScope.HISTORY),
    ],
)
@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [(FileNotFoundError, "GIT_NOT_AVAILABLE"), (OSError, "GIT_COMMAND_ERROR")],
)
def test_each_git_enumeration_failure_is_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    scope: ScanScope,
    raised: type[OSError],
    expected_code: str,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    original_run = secret_scan.subprocess.run

    def guarded_run(arguments: list[str], *args: object, **kwargs: object):
        selected = (
            (target == "working" and arguments[1:3] == ["ls-files", "-z"])
            or (target == "index" and arguments[1:3] == ["ls-files", "--stage"])
            or (target == "objects" and arguments[1] == "rev-list")
            or (target == "changes" and arguments[1] == "log")
        )
        if selected:
            raise raised()
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(secret_scan.subprocess, "run", guarded_run)

    report = scan_repository(root, scopes=(scope,))

    assert report.exit_code == 2
    assert expected_code in {error.code for error in report.errors}
    assert "Traceback" not in format_json(report) + format_text(report)


def test_git_output_limit_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _clean_repository(tmp_path / "repository")
    original_run = secret_scan.subprocess.run

    def oversized_run(arguments: list[str], *args: object, **kwargs: object):
        if arguments[1:3] == ["ls-files", "-z"]:
            return subprocess.CompletedProcess(arguments, 0, stdout=b"x" * 17, stderr=b"")
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(secret_scan.subprocess, "run", oversized_run)

    report = scan_repository(
        root,
        scopes=(ScanScope.WORKING_TREE,),
        limits=ScanLimits(max_git_output_bytes=16),
    )

    assert report.exit_code == 2
    assert report.errors[0].code == "GIT_OUTPUT_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("inspect", "FILE_INSPECTION_ERROR"),
        ("resolve", "PATH_OUTSIDE_REPOSITORY"),
        ("special", "SPECIAL_FILE_REJECTED"),
        ("read", "FILE_READ_ERROR"),
        ("growth", "CONTENT_LIMIT_EXCEEDED"),
    ],
)
def test_working_tree_file_failures_are_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    target = root / "problem.txt"
    target.write_text("safe\n", encoding="utf-8")
    original_lstat = Path.lstat
    original_resolve = Path.resolve
    original_open = Path.open

    if failure == "inspect":
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path: (
                (_ for _ in ()).throw(OSError()) if path == target else original_lstat(path)
            ),
        )
    elif failure == "resolve":
        monkeypatch.setattr(
            Path,
            "resolve",
            lambda path, *args, **kwargs: (
                (_ for _ in ()).throw(OSError())
                if path == target
                else original_resolve(path, *args, **kwargs)
            ),
        )
    elif failure == "special":
        monkeypatch.setattr(secret_scan.stat, "S_ISREG", lambda mode: False)
    elif failure == "read":
        monkeypatch.setattr(
            Path,
            "open",
            lambda path, *args, **kwargs: (
                (_ for _ in ()).throw(OSError())
                if path == target
                else original_open(path, *args, **kwargs)
            ),
        )
    else:
        monkeypatch.setattr(
            Path,
            "open",
            lambda path, *args, **kwargs: (
                io.BytesIO(b"x" * 33) if path == target else original_open(path, *args, **kwargs)
            ),
        )

    report = scan_repository(
        root,
        scopes=(ScanScope.WORKING_TREE,),
        limits=ScanLimits(max_content_bytes=32),
    )

    assert report.exit_code == 2
    assert expected_code in {error.code for error in report.errors}


def test_deleted_tracked_working_tree_file_is_not_an_error(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    (root / "safe.txt").unlink()

    report = scan_repository(root, scopes=(ScanScope.WORKING_TREE,))

    assert report.automatic_pass is True
    assert report.scopes[ScanScope.WORKING_TREE].scanned == 0


def test_index_symlink_is_incomplete(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    target = root / "safe.txt"
    link = root / "link.txt"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")
    subprocess.run(
        ["git", "add", "."],
        cwd=root,
        env=_git_environment(),
        check=True,
        shell=False,
    )

    report = scan_repository(root, scopes=(ScanScope.INDEX,))

    assert report.exit_code == 2
    assert "SYMLINK_REJECTED" in {error.code for error in report.errors}


def test_blob_binary_decode_and_size_limits_cover_index_and_history(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    (root / "binary.bin").write_bytes(b"binary\0value")
    (root / "legacy.txt").write_bytes(b"legacy-\xff-text")
    (root / "large.txt").write_bytes(b"x" * 33)
    _commit_all(root)

    report = scan_repository(
        root,
        scopes=(ScanScope.INDEX, ScanScope.HISTORY),
        limits=ScanLimits(max_content_bytes=32),
    )

    assert report.exit_code == 2
    assert report.scopes[ScanScope.INDEX].skipped_binary == 1
    assert report.scopes[ScanScope.HISTORY].skipped_binary == 1
    assert {error.code for error in report.errors} == {
        "CONTENT_LIMIT_EXCEEDED",
        "TEXT_DECODE_ERROR",
    }


@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [(FileNotFoundError, "GIT_NOT_AVAILABLE"), (OSError, "GIT_COMMAND_ERROR")],
)
def test_batch_object_inspection_spawn_failures_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: type[OSError],
    expected_code: str,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    original_popen = secret_scan.subprocess.Popen

    def failing_popen(arguments: list[str], *args: object, **kwargs: object):
        if arguments[1] == "cat-file":
            raise raised()
        return original_popen(arguments, *args, **kwargs)

    monkeypatch.setattr(secret_scan.subprocess, "Popen", failing_popen)

    report = scan_repository(root, scopes=(ScanScope.INDEX,))

    assert report.exit_code == 2
    assert report.errors[0].code == expected_code


def test_batch_object_inspection_timeout_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    original_popen = secret_scan.subprocess.Popen

    class TimedOutProcess:
        returncode = None
        calls = 0

        def communicate(self, payload: bytes | None = None, timeout: int | None = None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["git"], timeout or 0)
            return b"", b""

        def kill(self) -> None:
            return None

    def timed_out_popen(arguments: list[str], *args: object, **kwargs: object):
        if arguments[1] == "cat-file":
            return TimedOutProcess()
        return original_popen(arguments, *args, **kwargs)

    monkeypatch.setattr(secret_scan.subprocess, "Popen", timed_out_popen)

    report = scan_repository(root, scopes=(ScanScope.INDEX,))

    assert report.exit_code == 2
    assert report.errors[0].code == "GIT_COMMAND_ERROR"


def test_blob_reader_spawn_failure_after_metadata_is_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    original_popen = secret_scan.subprocess.Popen

    def guarded_popen(arguments: list[str], *args: object, **kwargs: object):
        if arguments[2] == "--batch":
            raise OSError
        return original_popen(arguments, *args, **kwargs)

    monkeypatch.setattr(secret_scan.subprocess, "Popen", guarded_popen)

    report = scan_repository(root, scopes=(ScanScope.INDEX,))

    assert report.exit_code == 2
    assert report.errors[0].code == "GIT_OBJECT_ERROR"


@pytest.mark.parametrize(
    ("path", "sensitive"),
    [
        (".env.local", True),
        (".env.template", False),
        ("nested/.npmrc", True),
        ("id_ed25519", True),
        ("private-key.pem", True),
        ("public.pem", False),
        ("client.crt", False),
        ("ordinary.py", False),
    ],
)
def test_sensitive_repository_path_variants(path: str, sensitive: bool) -> None:
    assert secret_scan._is_sensitive_repository_path(path) is sensitive


def test_internal_report_deduplication_warning_and_fixed_error_schema() -> None:
    scope = ScanScope.WORKING_TREE
    state = secret_scan._ScanState(
        ScanLimits(),
        {scope: secret_scan._MutableStatistics()},
    )
    state.add_finding("rule", scope, "safe.txt", "description")
    state.add_finding("rule", scope, "safe.txt", "description")
    state.add_warning("REVIEW", "Safe warning.", scope=scope, path="safe.txt")
    state.add_warning("REVIEW", "Safe warning.", scope=scope, path="safe.txt")
    report = secret_scan._build_report(state)
    failed = secret_scan.infrastructure_error_report((scope,), "SAFE_ERROR", "Safe error.")

    assert len(report.findings) == 1
    assert len(report.warnings) == 1
    assert "WARNING REVIEW scope=working_tree path=safe.txt" in format_text(report)
    assert failed.exit_code == 2
    assert json.loads(format_json(failed))["errors"] == [
        {"code": "SAFE_ERROR", "message": "Safe error."}
    ]


def test_root_must_be_directory_and_repository_top_level(tmp_path: Path) -> None:
    root = _clean_repository(tmp_path / "repository")
    file_root = root / "safe.txt"
    nested = root / "nested"
    nested.mkdir()

    file_report = scan_repository(file_root)
    nested_report = scan_repository(nested)

    assert file_report.errors[0].code == "ROOT_INVALID"
    assert nested_report.errors[0].code == "ROOT_NOT_TOPLEVEL"


def test_scanner_does_not_directly_open_git_objects_or_access_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _clean_repository(tmp_path / "repository")
    original_open = Path.open
    network_called = False

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if ".git" in path.parts:
            raise AssertionError("scanner must not directly open Git internals")
        return original_open(path, *args, **kwargs)

    def forbidden_network(*args: object, **kwargs: object) -> None:
        nonlocal network_called
        network_called = True
        raise AssertionError("scanner must remain offline")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)

    report = scan_repository(root)

    assert report.automatic_pass is True
    assert network_called is False
