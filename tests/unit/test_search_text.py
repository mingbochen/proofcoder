"""Offline semantics and subprocess-safety tests for search_text."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import BinaryIO

import pytest

import proofcoder.tools.search as search
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolResult
from proofcoder.tools.registry import ToolRegistry
from proofcoder.tools.search import (
    MAX_MATCH_LINE_CHARS,
    MAX_RIPGREP_STDERR_BYTES,
    MAX_RIPGREP_STDOUT_BYTES,
    MAX_SEARCH_FILE_SIZE_BYTES,
    RipgrepResolver,
    create_search_text_tool,
    minimal_subprocess_environment,
)


def _dispatch(
    workspace: Path,
    arguments: dict[str, object],
    *,
    environ: dict[str, str] | None = None,
    ripgrep_resolver: RipgrepResolver | None = None,
) -> ToolResult:
    registry = ToolRegistry()
    registry.register(
        create_search_text_tool(
            workspace,
            environ={} if environ is None else environ,
            ripgrep_resolver=ripgrep_resolver,
        )
    )
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


class _FakeProcess:
    pid = 424_242

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int | None = 0,
    ) -> None:
        self.stdout: BinaryIO = io.BytesIO(stdout)
        self.stderr: BinaryIO = io.BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.reaped = returncode is not None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("rg", timeout)
        self.reaped = True
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        self.terminated = True
        self.returncode = -signal_number

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


def _trusted_rg(workspace: Path) -> tuple[Path, dict[str, str]]:
    binary_directory = workspace.parent / f"{workspace.name}-trusted-bin"
    binary_directory.mkdir()
    candidate = binary_directory / ("rg.exe" if os.name == "nt" else "rg")
    candidate.write_bytes(b"offline-test-stub")
    if os.name != "nt":
        candidate.chmod(0o700)
    return candidate, {"PATH": str(binary_directory)}


def _match_event(line_number: int, text: str) -> bytes:
    return json.dumps(
        {
            "type": "match",
            "data": {"line_number": line_number, "lines": {"text": text}},
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _reap_fake_process(process: _FakeProcess, environment: object) -> None:
    del environment
    process.terminated = True
    process.returncode = -9
    process.reaped = True


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
    (tmp_path / "code.py").write_text("-AgentLoop", encoding="utf-8")
    candidate, environment = _trusted_rg(tmp_path)
    environment["DEEPSEEK_API_KEY"] = "fictional-test-key"
    environment["ORDINARY"] = "omitted"
    environment["RIPGREP_CONFIG_PATH"] = "untrusted-config"
    captured: dict[str, object] = {}

    def fake_popen(arguments: list[str], **kwargs: object) -> _FakeProcess:
        captured["arguments"] = arguments
        captured.update(kwargs)
        return _FakeProcess(stdout=_match_event(1, "-AgentLoop\n"))

    monkeypatch.setattr(search.subprocess, "Popen", fake_popen)

    result = _dispatch(
        tmp_path,
        {"query": "-AgentLoop", "case_sensitive": True},
        environ=environment,
    )

    arguments = captured["arguments"]
    assert isinstance(arguments, list)
    assert arguments[0] == str(candidate.resolve(strict=True))
    assert "--no-config" in arguments
    assert "--fixed-strings" in arguments
    assert arguments[-3:] == ["--", "-AgentLoop", "code.py"]
    assert captured["shell"] is False
    assert captured["cwd"] == tmp_path.resolve(strict=True)
    subprocess_environment = captured["env"]
    assert isinstance(subprocess_environment, dict)
    assert subprocess_environment == {"PATH": environment["PATH"]}
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    if os.name == "nt":
        assert "creationflags" in captured
    else:
        assert captured["start_new_session"] is True
    assert _matches(result)[0]["line"] == "-AgentLoop"


def test_ripgrep_and_python_fallback_return_the_same_match_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_bytes("alpha\r\nNeedle 雪\r\nneedle\r\n".encode())
    python_result = _dispatch(tmp_path, {"query": "needle"})
    _, environment = _trusted_rg(tmp_path)

    def fake_popen(arguments: list[str], **kwargs: object) -> _FakeProcess:
        del arguments, kwargs
        return _FakeProcess(
            stdout=b"\n".join(
                (
                    _match_event(2, "Needle 雪\r\n"),
                    _match_event(3, "needle\r\n"),
                )
            )
        )

    monkeypatch.setattr(search.subprocess, "Popen", fake_popen)
    ripgrep_result = _dispatch(tmp_path, {"query": "needle"}, environ=environment)

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


@pytest.mark.parametrize("path_value", ["", ".", "relative-bin", "workspace"])
def test_workspace_or_relative_path_never_executes_malicious_ripgrep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path_value: str,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    marker = tmp_path / "side-effect-marker"
    relative_bin = tmp_path / "relative-bin"
    relative_bin.mkdir()
    for name in ("rg", "rg.exe", "rg.cmd", "rg.bat", "rg.ps1"):
        malicious = tmp_path / name
        malicious.write_text(f"create {marker.name}", encoding="utf-8")
        if os.name != "nt":
            malicious.chmod(0o700)

    def fail_if_started(arguments: list[str], **kwargs: object) -> _FakeProcess:
        raise AssertionError(f"untrusted executable was started: {arguments!r}, {kwargs!r}")

    monkeypatch.setattr(search.subprocess, "Popen", fail_if_started)
    configured_path = str(tmp_path) if path_value == "workspace" else path_value

    result = _dispatch(tmp_path, {"query": "needle"}, environ={"PATH": configured_path})

    assert [item["path"] for item in _matches(result)] == ["code.py"]
    assert not marker.exists()


@pytest.mark.parametrize(
    "candidate_kind",
    [
        "relative",
        "workspace",
        "not-on-path",
        "missing",
        "directory",
        "rg.cmd",
        "rg.bat",
        "rg.ps1",
    ],
)
def test_invalid_injected_resolver_candidates_fall_back_without_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    binary_directory = tmp_path.parent / f"{tmp_path.name}-invalid-{candidate_kind}"
    binary_directory.mkdir()
    expected_name = "rg.exe" if os.name == "nt" else "rg"
    environment = {"PATH": str(binary_directory)}

    if candidate_kind == "relative":
        candidate = Path(expected_name)
    elif candidate_kind == "workspace":
        candidate = tmp_path / expected_name
        candidate.write_bytes(b"workspace executable")
        candidate.chmod(0o700)
        environment = {"PATH": str(tmp_path)}
    elif candidate_kind == "not-on-path":
        candidate_directory = tmp_path.parent / f"{tmp_path.name}-not-on-path"
        candidate_directory.mkdir()
        candidate = candidate_directory / expected_name
        candidate.write_bytes(b"external but not operator configured")
        candidate.chmod(0o700)
    elif candidate_kind == "missing":
        candidate = binary_directory / expected_name
    elif candidate_kind == "directory":
        candidate = binary_directory / expected_name
        candidate.mkdir()
    else:
        candidate = binary_directory / candidate_kind
        candidate.write_bytes(b"script wrapper")
        candidate.chmod(0o700)

    def fail_if_started(arguments: list[str], **kwargs: object) -> _FakeProcess:
        raise AssertionError(f"invalid resolver result was started: {arguments!r}, {kwargs!r}")

    monkeypatch.setattr(search.subprocess, "Popen", fail_if_started)
    result = _dispatch(
        tmp_path,
        {"query": "needle"},
        environ=environment,
        ripgrep_resolver=lambda workspace, sanitized: candidate,
    )

    assert [item["path"] for item in _matches(result)] == ["code.py"]


def test_default_resolver_skips_unsafe_path_entries_before_trusted_absolute_directory(
    tmp_path: Path,
) -> None:
    candidate, environment = _trusted_rg(tmp_path)
    environment["PATH"] = os.pathsep.join(
        ("", ".", "relative-bin", str(tmp_path), environment["PATH"])
    )

    resolved = search._resolve_ripgrep_from_path(tmp_path.resolve(strict=True), environment)

    assert resolved == candidate


def test_resolver_rejects_external_symlink_whose_target_is_in_workspace(tmp_path: Path) -> None:
    binary_directory = tmp_path.parent / f"{tmp_path.name}-symlink-bin"
    binary_directory.mkdir()
    expected_name = "rg.exe" if os.name == "nt" else "rg"
    workspace_target = tmp_path / expected_name
    workspace_target.write_bytes(b"workspace target")
    workspace_target.chmod(0o700)
    candidate = binary_directory / expected_name
    try:
        candidate.symlink_to(workspace_target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert (
        search._validate_ripgrep_candidate(
            tmp_path.resolve(strict=True),
            candidate,
            {"PATH": str(binary_directory)},
        )
        is None
    )


def test_resolver_allows_external_symlink_only_after_external_final_resolution(
    tmp_path: Path,
) -> None:
    binary_directory = tmp_path.parent / f"{tmp_path.name}-symlink-bin"
    target_directory = tmp_path.parent / f"{tmp_path.name}-symlink-target"
    binary_directory.mkdir()
    target_directory.mkdir()
    expected_name = "rg.exe" if os.name == "nt" else "rg"
    target = target_directory / ("actual.exe" if os.name == "nt" else "actual")
    target.write_bytes(b"external target")
    target.chmod(0o700)
    candidate = binary_directory / expected_name
    try:
        candidate.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert search._validate_ripgrep_candidate(
        tmp_path.resolve(strict=True),
        candidate,
        {"PATH": str(binary_directory)},
    ) == str(target.resolve(strict=True))


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_windows_workspace_comparison_is_case_insensitive(tmp_path: Path) -> None:
    candidate = tmp_path / "rg.exe"
    candidate.write_bytes(b"workspace executable")
    swapped_case = Path(str(candidate).swapcase())

    assert (
        search._validate_ripgrep_candidate(
            tmp_path.resolve(strict=True),
            swapped_case,
            {"PATH": str(tmp_path).swapcase()},
        )
        is None
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX executables require an execute bit")
def test_posix_resolver_rejects_non_executable_regular_file(tmp_path: Path) -> None:
    binary_directory = tmp_path.parent / f"{tmp_path.name}-non-executable-bin"
    binary_directory.mkdir()
    candidate = binary_directory / "rg"
    candidate.write_bytes(b"not executable")
    candidate.chmod(0o600)

    assert (
        search._validate_ripgrep_candidate(
            tmp_path.resolve(strict=True),
            candidate,
            {"PATH": str(binary_directory)},
        )
        is None
    )


def test_ripgrep_startup_and_nonzero_failures_use_safe_python_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    _, environment = _trusted_rg(tmp_path)

    def fail_to_start(arguments: list[str], **kwargs: object) -> _FakeProcess:
        del arguments, kwargs
        raise OSError("fictional-sensitive-startup-detail")

    monkeypatch.setattr(search.subprocess, "Popen", fail_to_start)
    startup_result = _dispatch(tmp_path, {"query": "needle"}, environ=environment)

    monkeypatch.setattr(
        search.subprocess,
        "Popen",
        lambda arguments, **kwargs: _FakeProcess(
            returncode=3,
            stderr=b"fictional-sensitive-stderr",
        ),
    )
    nonzero_result = _dispatch(tmp_path, {"query": "needle"}, environ=environment)

    assert [item["path"] for item in _matches(startup_result)] == ["code.py"]
    assert [item["path"] for item in _matches(nonzero_result)] == ["code.py"]
    assert "fictional-sensitive" not in startup_result.to_json()
    assert "fictional-sensitive" not in nonzero_result.to_json()


def test_ripgrep_timeout_terminates_reaps_and_falls_back_without_raw_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    _, environment = _trusted_rg(tmp_path)
    process = _FakeProcess(
        returncode=None,
        stderr=b"fictional-sensitive-timeout-stderr",
    )
    monkeypatch.setattr(search, "RIPGREP_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(search, "_RIPGREP_POLL_SECONDS", 0.0001)
    monkeypatch.setattr(search, "_terminate_process_tree", _reap_fake_process)
    monkeypatch.setattr(search.subprocess, "Popen", lambda arguments, **kwargs: process)

    result = _dispatch(tmp_path, {"query": "needle"}, environ=environment)

    assert [item["path"] for item in _matches(result)] == ["code.py"]
    assert process.terminated is True
    assert process.reaped is True
    assert "fictional-sensitive-timeout-stderr" not in result.to_json()


@pytest.mark.parametrize(
    ("stream_name", "limit_name", "raw_output"),
    [
        ("stdout", "MAX_RIPGREP_STDOUT_BYTES", b"stdout-sensitive-overflow"),
        ("stderr", "MAX_RIPGREP_STDERR_BYTES", b"stderr-sensitive-overflow"),
    ],
)
def test_each_ripgrep_stream_has_an_independent_raw_byte_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_name: str,
    limit_name: str,
    raw_output: bytes,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    _, environment = _trusted_rg(tmp_path)
    monkeypatch.setattr(search, limit_name, 16)
    streams = {"stdout": b"", "stderr": b""}
    streams[stream_name] = raw_output
    process = _FakeProcess(returncode=None, **streams)
    monkeypatch.setattr(search, "_terminate_process_tree", _reap_fake_process)
    monkeypatch.setattr(search.subprocess, "Popen", lambda arguments, **kwargs: process)

    result = _dispatch(tmp_path, {"query": "needle"}, environ=environment)

    assert [item["path"] for item in _matches(result)] == ["code.py"]
    assert process.terminated is True
    assert process.reaped is True
    assert raw_output.decode() not in result.to_json()


def test_bounded_capture_never_retains_more_than_its_raw_byte_limit() -> None:
    capture = search._BoundedBytes(limit=4)

    assert capture.append(b"123") is False
    assert capture.append(b"456789") is True
    assert capture.total_bytes == 9
    assert bytes(capture.retained) == b"1234"
    assert len(capture.retained) <= capture.limit


def test_ripgrep_invalid_json_falls_back_without_exposing_backend_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "code.py").write_text("needle", encoding="utf-8")
    _, environment = _trusted_rg(tmp_path)
    monkeypatch.setattr(
        search.subprocess,
        "Popen",
        lambda arguments, **kwargs: _FakeProcess(stdout=b"not-json-sensitive-output"),
    )

    result = _dispatch(tmp_path, {"query": "needle"}, environ=environment)

    assert [item["path"] for item in _matches(result)] == ["code.py"]
    assert "not-json-sensitive-output" not in result.to_json()


def test_raw_ripgrep_limits_are_finite_and_separate() -> None:
    assert 0 < MAX_RIPGREP_STDERR_BYTES < MAX_RIPGREP_STDOUT_BYTES
