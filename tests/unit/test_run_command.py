"""Offline execution tests for run_command using only temporary workspace scripts."""

from __future__ import annotations

import ctypes
import io
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

import proofcoder.tools.command as command_tool
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.safety.commands import prepare_command
from proofcoder.tools.base import ToolResult
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.files import create_list_files_tool, create_read_file_tool
from proofcoder.tools.registry import ToolRegistry

FAKE_SECRET = "obviously-fake-output-secret-for-tests"


def _environment(workspace: Path, **extra: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", str(Path(sys.executable).resolve().parent)),
        "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": str(workspace),
        "TMP": str(workspace),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
    }
    environment.update(extra)
    return environment


def _dispatch(
    workspace: Path,
    argv: list[str],
    *,
    timeout_seconds: int = 10,
    environ: dict[str, str] | None = None,
) -> ToolResult:
    registry = ToolRegistry()
    registry.register(
        create_run_command_tool(workspace, environ=environ or _environment(workspace))
    )
    return registry.dispatch(
        ToolCall(
            id="command-1",
            function=FunctionCall(
                name="run_command",
                arguments=json.dumps(
                    {
                        "argv": argv,
                        "cwd": ".",
                        "timeout_seconds": timeout_seconds,
                    }
                ),
            ),
        )
    )


def _write_script(workspace: Path, name: str, source: str) -> Path:
    path = workspace / name
    path.write_text(source, encoding="utf-8")
    return path


def _data(result: ToolResult) -> dict[str, object]:
    assert result.data is not None
    return result.data


def _audit_payload(workspace: Path, result: ToolResult) -> dict[str, object]:
    audit_path = _data(result)["audit_path"]
    assert isinstance(audit_path, str)
    path = workspace / Path(audit_path)
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_is_alive(pid)


def _force_stop_pid(pid: int) -> None:
    if not _pid_is_alive(pid):
        return
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x0001, False, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if _wait_for_pid_exit(pid, timeout=1.0):
        return
    if hasattr(signal, "SIGKILL"):
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def test_success_separates_streams_records_exit_zero_and_creates_audit(tmp_path: Path) -> None:
    _write_script(
        tmp_path,
        "streams.py",
        "import sys\nprint('stdout-value')\nprint('stderr-value', file=sys.stderr)\n",
    )

    result = _dispatch(tmp_path, ["python", "streams.py"])

    assert result.ok is True
    data = _data(result)
    assert data["argv"] == ["python", "streams.py"]
    assert data["cwd"] == "."
    assert data["command_kind"] == "script"
    assert data["exit_code"] == 0
    assert data["stdout"] == "stdout-value\n"
    assert data["stderr"] == "stderr-value\n"
    assert data["stdout_bytes"] == len("stdout-value\r\n" if os.name == "nt" else "stdout-value\n")
    assert data["timed_out"] is False
    assert data["stdout_truncated"] is False
    assert data["stderr_truncated"] is False
    assert result.meta.truncated is False
    assert result.meta.duration_ms >= 0
    audit = _audit_payload(tmp_path, result)
    assert audit["exit_code"] == 0
    assert audit["stdout"] == "stdout-value\n"
    assert audit["stderr"] == "stderr-value\n"
    assert str(Path(sys.executable).resolve()) not in json.dumps(audit)


def test_nonzero_exit_is_a_successful_recoverable_observation(tmp_path: Path) -> None:
    _write_script(
        tmp_path,
        "fails.py",
        "import sys\nprint('assertion failed', file=sys.stderr)\nraise SystemExit(7)\n",
    )

    result = _dispatch(tmp_path, ["python", "fails.py"])

    assert result.ok is True
    assert result.error is None
    assert _data(result)["exit_code"] == 7
    assert _data(result)["stderr"] == "assertion failed\n"


def test_popen_receives_argv_shell_false_no_stdin_and_filtered_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_script(tmp_path, "ok.py", "raise SystemExit(0)\n")
    original_popen = command_tool.subprocess.Popen
    observed: dict[str, object] = {}

    def observing_popen(*args: object, **kwargs: object):
        observed["args"] = args
        observed.update(kwargs)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(command_tool.subprocess, "Popen", observing_popen)
    environment = _environment(
        tmp_path,
        DEEPSEEK_API_KEY=FAKE_SECRET,
        SERVICE_TOKEN="fake-token",
        ORDINARY_PARENT_VALUE="not-required",
    )

    result = _dispatch(tmp_path, ["python", "ok.py"], environ=environment)

    assert result.ok is True
    assert observed["shell"] is False
    assert observed["stdin"] is command_tool.subprocess.DEVNULL
    assert isinstance(observed["args"], tuple)
    process_argv = observed["args"][0]
    assert isinstance(process_argv, list)
    assert process_argv[0] == sys.executable
    child_environment = observed["env"]
    assert isinstance(child_environment, dict)
    assert "DEEPSEEK_API_KEY" not in child_environment
    assert "SERVICE_TOKEN" not in child_environment
    assert "ORDINARY_PARENT_VALUE" not in child_environment
    if os.name == "nt":
        assert observed["creationflags"] & command_tool.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert observed["start_new_session"] is True


def test_child_process_cannot_observe_secret_like_or_full_parent_environment(
    tmp_path: Path,
) -> None:
    _write_script(
        tmp_path,
        "environment.py",
        "import json, os\nprint(json.dumps(dict(sorted(os.environ.items()))))\n",
    )
    environment = _environment(
        tmp_path,
        DEEPSEEK_API_KEY=FAKE_SECRET,
        BUILD_TOKEN="fake-token",
        CLIENT_SECRET="fake-secret",
        USER_PASSWORD="fake-password",
        CLOUD_CREDENTIAL="fake-credential",
        ORDINARY_PARENT_VALUE="ordinary-parent",
    )

    result = _dispatch(tmp_path, ["python", "environment.py"], environ=environment)

    child_environment = json.loads(str(_data(result)["stdout"]))
    assert "PATH" in child_environment
    assert child_environment["PYTHONUTF8"] == "1"
    assert child_environment["GIT_TERMINAL_PROMPT"] == "0"
    for excluded in (
        "DEEPSEEK_API_KEY",
        "BUILD_TOKEN",
        "CLIENT_SECRET",
        "USER_PASSWORD",
        "CLOUD_CREDENTIAL",
        "ORDINARY_PARENT_VALUE",
    ):
        assert excluded not in child_environment


def test_output_is_redacted_and_terminal_controls_are_removed_from_return_and_audit(
    tmp_path: Path,
) -> None:
    _write_script(
        tmp_path,
        "unsafe_output.py",
        "import sys\n"
        f"sys.stdout.write('\\x1b[31mred\\x1b[0m\\x07{FAKE_SECRET}\\n')\n"
        "sys.stderr.write('safe\\x08stderr\\n')\n",
    )

    result = _dispatch(
        tmp_path,
        ["python", "unsafe_output.py"],
        environ=_environment(tmp_path, DEEPSEEK_API_KEY=FAKE_SECRET),
    )

    serialized_result = result.to_json()
    audit = _audit_payload(tmp_path, result)
    serialized_audit = json.dumps(audit)
    for serialized in (serialized_result, serialized_audit):
        assert FAKE_SECRET not in serialized
        assert "\\u001b" not in serialized
        assert "\\u0007" not in serialized
        assert "\\u0008" not in serialized
    assert _data(result)["stdout"] == "red[redacted]\n"
    assert _data(result)["stderr"] == "safestderr\n"


def test_stdout_and_stderr_are_independently_head_tail_truncated(tmp_path: Path) -> None:
    _write_script(
        tmp_path,
        "large_output.py",
        "import sys\n"
        "sys.stdout.write('OUT-HEAD-' + 'o' * 40000 + '-OUT-TAIL')\n"
        "sys.stderr.write('ERR-HEAD-' + 'e' * 40000 + '-ERR-TAIL')\n",
    )

    result = _dispatch(tmp_path, ["python", "large_output.py"])

    data = _data(result)
    stdout = str(data["stdout"])
    stderr = str(data["stderr"])
    assert stdout.startswith("OUT-HEAD-")
    assert stdout.endswith("-OUT-TAIL")
    assert stderr.startswith("ERR-HEAD-")
    assert stderr.endswith("-ERR-TAIL")
    assert "output truncated:" in stdout
    assert "output truncated:" in stderr
    assert len(stdout.encode()) <= command_tool.MAX_RETURN_STREAM_BYTES
    assert len(stderr.encode()) <= command_tool.MAX_RETURN_STREAM_BYTES
    assert data["stdout_truncated"] is True
    assert data["stderr_truncated"] is True
    assert result.meta.truncated is True
    audit = _audit_payload(tmp_path, result)
    assert "output truncated:" not in str(audit["stdout"])
    assert "OUT-HEAD-" in str(audit["stdout"])
    assert str(audit["stdout"]).endswith("-OUT-TAIL")


def test_audit_stream_hard_limit_is_bounded_and_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(command_tool, "MAX_AUDIT_STREAM_BYTES", 1024)
    _write_script(
        tmp_path,
        "audit_limit.py",
        "import sys\nsys.stdout.write('HEAD-' + 'x' * 5000 + '-TAIL')\n",
    )

    result = _dispatch(tmp_path, ["python", "audit_limit.py"])

    data = _data(result)
    assert data["stdout_bytes"] == 5010
    assert data["stdout_truncated"] is True
    assert data["audit_truncated"] is True
    assert result.meta.truncated is True
    assert any("AUDIT_OUTPUT_TRUNCATED" in warning for warning in result.meta.warnings)
    audit = _audit_payload(tmp_path, result)
    assert "audit stream truncated:" in str(audit["stdout"])
    assert str(audit["stdout"]).startswith("HEAD-")
    assert str(audit["stdout"]).endswith("-TAIL")


def test_audit_write_failure_is_a_warning_without_losing_command_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_script(tmp_path, "ok.py", "print('still available')\n")

    def fail_audit(*args: object, **kwargs: object) -> str:
        raise OSError("simulated audit failure")

    monkeypatch.setattr(command_tool, "_write_audit_file", fail_audit)

    result = _dispatch(tmp_path, ["python", "ok.py"])

    assert result.ok is True
    assert _data(result)["exit_code"] == 0
    assert _data(result)["stdout"] == "still available\n"
    assert _data(result)["audit_path"] is None
    assert any("AUDIT_WRITE_FAILED" in warning for warning in result.meta.warnings)


@pytest.mark.parametrize(
    "exception,code",
    [(FileNotFoundError(), "COMMAND_NOT_FOUND"), (OSError(), "COMMAND_SPAWN_ERROR")],
)
def test_spawn_failures_are_structured_without_tracebacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exception: OSError,
    code: str,
) -> None:
    _write_script(tmp_path, "ok.py", "raise SystemExit(0)\n")

    def fail_spawn(*args: object, **kwargs: object) -> None:
        raise exception

    monkeypatch.setattr(command_tool.subprocess, "Popen", fail_spawn)

    result = _dispatch(tmp_path, ["python", "ok.py"])

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == code
    assert "Traceback" not in result.to_json()


def test_timeout_returns_output_and_terminates_direct_process_without_raw_temporary_files(
    tmp_path: Path,
) -> None:
    _write_script(
        tmp_path,
        "timeout.py",
        "import os, pathlib, time\n"
        "pathlib.Path('direct.pid').write_text(str(os.getpid()), encoding='utf-8')\n"
        "print('before-timeout', flush=True)\n"
        "time.sleep(30)\n",
    )

    result = _dispatch(tmp_path, ["python", "timeout.py"], timeout_seconds=1)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "COMMAND_TIMEOUT"
    data = _data(result)
    assert data["timed_out"] is True
    assert data["stdout"] == "before-timeout\n"
    assert data["exit_code"] is not None
    assert result.meta.duration_ms >= 1000
    pid = int((tmp_path / "direct.pid").read_text(encoding="utf-8"))
    assert _wait_for_pid_exit(pid)
    audit = _audit_payload(tmp_path, result)
    assert audit["timed_out"] is True
    audit_directory = tmp_path / ".proofcoder" / "runtime" / "commands"
    assert all(path.suffix == ".json" for path in audit_directory.iterdir())


def test_timeout_best_effort_cleans_child_process_tree(tmp_path: Path) -> None:
    _write_script(
        tmp_path,
        "tree.py",
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid), encoding='utf-8')\n"
        "print('tree-started', flush=True)\n"
        "time.sleep(30)\n",
    )

    result = _dispatch(tmp_path, ["python", "tree.py"], timeout_seconds=1)

    assert result.error is not None
    assert result.error.code == "COMMAND_TIMEOUT"
    child_pid = int((tmp_path / "child.pid").read_text(encoding="utf-8"))
    if not _wait_for_pid_exit(child_pid):
        _force_stop_pid(child_pid)
        pytest.skip("this platform could not reliably verify descendant process cleanup")


@pytest.mark.parametrize("raised", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_propagate_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raised: type[BaseException],
) -> None:
    _write_script(tmp_path, "unused.py", "raise SystemExit(0)\n")
    command = prepare_command(
        tmp_path,
        {"argv": ["python", "unused.py"]},
        environ=_environment(tmp_path),
    )
    cleaned = False

    class InterruptingProcess:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = None

        def wait(self, timeout: float | None = None) -> None:
            raise raised()

        def poll(self) -> None:
            return None

    def fake_popen(*args: object, **kwargs: object) -> InterruptingProcess:
        return InterruptingProcess()

    def record_cleanup(
        process: command_tool.subprocess.Popen[bytes],
        environment: dict[str, str],
    ) -> None:
        nonlocal cleaned
        cleaned = True

    monkeypatch.setattr(command_tool.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(command_tool, "_terminate_process_tree", record_cleanup)

    with pytest.raises(raised):
        command_tool._run_prepared_command(tmp_path, command)

    assert cleaned is True


def test_internal_audit_directory_is_hidden_from_normal_file_tools(tmp_path: Path) -> None:
    _write_script(tmp_path, "ok.py", "print('ok')\n")
    result = _dispatch(tmp_path, ["python", "ok.py"])
    audit_path = _data(result)["audit_path"]
    assert isinstance(audit_path, str)

    registry = ToolRegistry()
    registry.register(create_list_files_tool(tmp_path))
    registry.register(create_read_file_tool(tmp_path))
    list_result = registry.dispatch(
        ToolCall(
            id="list",
            function=FunctionCall(name="list_files", arguments="{}"),
        )
    )
    read_result = registry.dispatch(
        ToolCall(
            id="read",
            function=FunctionCall(
                name="read_file",
                arguments=json.dumps({"path": audit_path}),
            ),
        )
    )

    assert list_result.ok is True
    assert ".proofcoder" not in json.dumps(list_result.to_dict())
    assert read_result.ok is False
    assert read_result.error is not None
    assert read_result.error.code == "SENSITIVE_PATH"
