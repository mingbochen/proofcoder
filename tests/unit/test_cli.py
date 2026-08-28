"""Offline tests for doctor and both CLI entry points."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from rich.console import Console

import proofcoder.cli as cli
from proofcoder.config import ProofCoderConfig
from proofcoder.errors import DeepSeekAPIError
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall

SENSITIVE_SENTINEL = "never-print-this-value"
REASONING_SENTINEL = "internal-reasoning-must-stay-private"


class _SuccessClient:
    def check_connection(self) -> ModelResponse:
        return ModelResponse(
            content="response-content-is-not-doctor-output",
            reasoning_content=REASONING_SENTINEL,
            finish_reason="stop",
            usage=None,
        )


class _FailureClient:
    def check_connection(self) -> ModelResponse:
        raise DeepSeekAPIError(f"unsafe detail: {SENSITIVE_SENTINEL}")


def _run_cli(
    argv: list[str],
    *,
    environ: Mapping[str, str],
    cwd: Path,
    client_factory: Callable[[ProofCoderConfig], object],
) -> tuple[int, str]:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=120)
    code = cli.main(
        argv,
        environ=environ,
        cwd=cwd,
        console=console,
        client_factory=client_factory,
    )
    return code, stream.getvalue()


def test_doctor_offline_passes_without_creating_client(tmp_path: Path) -> None:
    called = False

    def forbidden_factory(config: ProofCoderConfig) -> object:
        nonlocal called
        called = True
        raise AssertionError(config)

    code, output = _run_cli(
        ["doctor", "--offline"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=forbidden_factory,
    )

    assert code == 0
    assert called is False
    assert "skipped in offline mode" in output
    assert SENSITIVE_SENTINEL not in output


def test_online_doctor_success_hides_response_and_reasoning(tmp_path: Path) -> None:
    def factory(config: ProofCoderConfig) -> _SuccessClient:
        assert config.api_key == SENSITIVE_SENTINEL
        return _SuccessClient()

    code, output = _run_cli(
        ["doctor"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=factory,
    )

    assert code == 0
    assert "connection succeeded" in output
    assert SENSITIVE_SENTINEL not in output
    assert REASONING_SENTINEL not in output
    assert "response-content-is-not-doctor-output" not in output


def test_online_doctor_failure_is_safe_and_nonzero(tmp_path: Path) -> None:
    def factory(config: ProofCoderConfig) -> _FailureClient:
        assert config.api_key == SENSITIVE_SENTINEL
        return _FailureClient()

    code, output = _run_cli(
        ["doctor"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=factory,
    )

    assert code == 1
    assert "request failed" in output
    assert SENSITIVE_SENTINEL not in output


def test_missing_online_api_key_is_a_clear_configuration_error(tmp_path: Path) -> None:
    code, output = _run_cli(
        ["doctor"],
        environ={},
        cwd=tmp_path,
        client_factory=lambda config: _SuccessClient(),
    )

    assert code == 1
    assert "DEEPSEEK_API_KEY is required" in output


def test_doctor_redacts_secret_from_other_displayed_configuration(tmp_path: Path) -> None:
    code, output = _run_cli(
        ["doctor"],
        environ={
            "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
            "DEEPSEEK_MODEL": f"model-{SENSITIVE_SENTINEL}",
        },
        cwd=tmp_path,
        client_factory=lambda config: _SuccessClient(),
    )

    assert code == 0
    assert SENSITIVE_SENTINEL not in output
    assert "model-[redacted]" in output


def test_failed_local_check_prevents_network_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False

    def forbidden_factory(config: ProofCoderConfig) -> object:
        nonlocal called
        called = True
        raise AssertionError(config)

    monkeypatch.setattr(cli.os, "access", lambda path, mode: False)
    code, output = _run_cli(
        ["doctor"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        client_factory=forbidden_factory,
    )

    assert code == 1
    assert called is False
    assert "FAIL Working directory" in output


def test_package_import_failure_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )
    code, output = _run_cli(
        ["doctor", "--offline"],
        environ={},
        cwd=tmp_path,
        client_factory=lambda config: _SuccessClient(),
    )

    assert code == 1
    assert "FAIL ProofCoder import" in output


@pytest.mark.parametrize("module_entry", [True, False])
def test_help_is_available_from_both_entry_points(module_entry: bool) -> None:
    if module_entry:
        command = [sys.executable, "-m", "proofcoder", "--help"]
    else:
        executable = shutil.which("proofcoder")
        assert executable is not None
        command = [executable, "--help"]

    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert "doctor" in result.stdout
    assert "run" in result.stdout


def _scripted_response(
    *,
    content: str | None = None,
    calls: tuple[ToolCall, ...] = (),
) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=REASONING_SENTINEL,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _list_call(call_id: str = "list-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name="list_files", arguments="{}"),
    )


def test_run_cli_uses_scripted_client_and_hides_reasoning(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("", encoding="utf-8")
    scripted = ScriptedClient(
        [
            _scripted_response(calls=(_list_call(),)),
            _scripted_response(content="Workspace listed."),
        ]
    )
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=200)

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "inspect"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=console,
        run_client_factory=lambda config: scripted,
    )
    output = stream.getvalue()

    assert code == 1
    for label in ("TASK:", "MODEL:", "TOOL:", "RESULT:", "DONE:"):
        assert label in output
    assert "termination=model_stopped" in output
    assert "verified" not in output
    assert REASONING_SENTINEL not in output
    assert SENSITIVE_SENTINEL not in output
    assert [tool["function"]["name"] for tool in scripted.requests[0].tools] == [
        "list_files",
        "search_text",
        "read_file",
        "create_file",
        "replace_in_file",
        "run_command",
        "finish_task",
    ]


def test_run_cli_displays_bounded_write_result_without_reasoning(tmp_path: Path) -> None:
    create_call = ToolCall(
        id="create-1",
        function=FunctionCall(
            name="create_file",
            arguments='{"path":"created.txt","content":"hello\\n"}',
        ),
    )
    scripted = ScriptedClient(
        [
            _scripted_response(calls=(create_call,)),
            _scripted_response(content="Modified but not verified by the Agent."),
        ]
    )
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "create a file"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None, width=200),
        run_client_factory=lambda config: scripted,
    )
    output = stream.getvalue()

    assert code == 1
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "hello\n"
    assert '"ok":true' in output
    assert '"diff":"--- /dev/null\\n+++ b/created.txt\\n' in output
    assert '"truncated":false' in output
    assert REASONING_SENTINEL not in output


def test_run_cli_displays_command_observation_without_reasoning_or_secret(tmp_path: Path) -> None:
    (tmp_path / "cli_check.py").write_text(
        "import sys\nprint('cli-stdout')\nprint('cli-stderr', file=sys.stderr)\n",
        encoding="utf-8",
    )
    command_call = ToolCall(
        id="command-1",
        function=FunctionCall(
            name="run_command",
            arguments='{"argv":["python","cli_check.py"],"timeout_seconds":10}',
        ),
    )
    scripted = ScriptedClient(
        [
            _scripted_response(calls=(command_call,)),
            _scripted_response(content="Ran python cli_check.py with exit code 0."),
        ]
    )
    stream = io.StringIO()
    environment = {
        "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
        "PATH": str(Path(sys.executable).resolve().parent),
        "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": str(tmp_path),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
    }

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "run the check"],
        environ=environment,
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None, width=240),
        run_client_factory=lambda config: scripted,
    )
    output = stream.getvalue()

    assert code == 1
    assert "TOOL: run_command" in output
    assert '"command_kind":"script"' in output
    assert '"exit_code":0' in output
    assert '"stdout_truncated":false' in output
    assert '"stderr_truncated":false' in output
    assert '"audit_path":".proofcoder/runtime/commands/' in output
    assert "cli-stdout" in output
    assert "cli-stderr" in output
    assert REASONING_SENTINEL not in output
    assert SENSITIVE_SENTINEL not in output


def test_run_cli_displays_stable_blocked_command_code(tmp_path: Path) -> None:
    blocked_call = ToolCall(
        id="blocked-1",
        function=FunctionCall(
            name="run_command",
            arguments='{"argv":["python","-c","print(1)"]}',
        ),
    )
    scripted = ScriptedClient(
        [
            _scripted_response(calls=(blocked_call,)),
            _scripted_response(content="The unsafe command was not run."),
        ]
    )
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "do not bypass policy"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None, width=200),
        run_client_factory=lambda config: scripted,
    )
    output = stream.getvalue()

    assert code == 1
    assert "TOOL: run_command" in output
    assert "COMMAND_BLOCKED" in output
    assert REASONING_SENTINEL not in output
    assert SENSITIVE_SENTINEL not in output


def test_run_cli_max_steps_has_distinct_exit_code(tmp_path: Path) -> None:
    scripted = ScriptedClient([_scripted_response(calls=(_list_call(),))])
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "--max-steps", "1", "inspect"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None),
        run_client_factory=lambda config: scripted,
    )

    assert code == 1
    assert "termination=max_steps" in stream.getvalue()


def test_run_cli_rejects_missing_workspace_without_model_call(tmp_path: Path) -> None:
    called = False

    def forbidden(config: ProofCoderConfig) -> ScriptedClient:
        nonlocal called
        called = True
        return ScriptedClient([])

    stream = io.StringIO()
    code = cli.main(
        ["run", "--workspace", "missing", "inspect"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None),
        run_client_factory=forbidden,
    )

    assert code == 2
    assert called is False
    assert "termination=invalid_workspace" in stream.getvalue()


def test_run_cli_script_exhaustion_is_api_error(tmp_path: Path) -> None:
    scripted = ScriptedClient([_scripted_response(calls=(_list_call(),))])
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "--max-steps", "2", "inspect"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None),
        run_client_factory=lambda config: scripted,
    )

    assert code == 1
    assert "termination=api_error" in stream.getvalue()


def test_run_help_is_available_without_configuration() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "proofcoder", "run", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--workspace" in result.stdout
    assert "--max-steps" in result.stdout


def _finish_call(arguments: dict[str, object], call_id: str = "finish-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name="finish_task", arguments=json.dumps(arguments)),
    )


def test_run_cli_verified_completion_has_evidence_and_exit_zero(tmp_path: Path) -> None:
    (tmp_path / "subject.py").write_text('VALUE = "broken"\n', encoding="utf-8")
    (tmp_path / "test_subject.py").write_text(
        "import unittest\n"
        "import subject\n\n"
        "class SubjectTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(subject.VALUE, 'fixed')\n",
        encoding="utf-8",
    )
    verification_argv = ["python", "-m", "unittest", "-q"]
    edit_call = ToolCall(
        id="edit-1",
        function=FunctionCall(
            name="replace_in_file",
            arguments=('{"path":"subject.py","old_text":"broken","new_text":"fixed"}'),
        ),
    )
    command_call = ToolCall(
        id="verify-1",
        function=FunctionCall(
            name="run_command",
            arguments=('{"argv":["python","-m","unittest","-q"],"timeout_seconds":10}'),
        ),
    )
    scripted = ScriptedClient(
        [
            _scripted_response(calls=(edit_call,)),
            _scripted_response(calls=(command_call,)),
            _scripted_response(
                calls=(
                    _finish_call(
                        {
                            "summary": "fixed and verified",
                            "changed_files": ["subject.py"],
                            "verification_command": verification_argv,
                            "limitations": [],
                        }
                    ),
                )
            ),
        ]
    )
    environment = {
        "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
        "PATH": str(Path(sys.executable).resolve().parent),
        "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
    }
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "fix and verify"],
        environ=environment,
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None, width=300),
        run_client_factory=lambda config: scripted,
    )
    output = stream.getvalue()

    assert code == 0
    assert "termination=finish_task" in output
    assert "completion=completed_verified" in output
    assert 'changed_files=["subject.py"]' in output
    assert 'verification_argv=["python","-m","unittest","-q"]' in output
    assert "verification_exit_code=0" in output
    assert REASONING_SENTINEL not in output
    assert SENSITIVE_SENTINEL not in output


def test_run_cli_unverified_completion_has_exit_three(tmp_path: Path) -> None:
    create_call = ToolCall(
        id="create-1",
        function=FunctionCall(
            name="create_file",
            arguments='{"path":"created.txt","content":"content"}',
        ),
    )
    scripted = ScriptedClient(
        [
            _scripted_response(calls=(create_call,)),
            _scripted_response(
                calls=(_finish_call({"summary": "created", "changed_files": ["created.txt"]}),)
            ),
        ]
    )
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "create"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None),
        run_client_factory=lambda config: scripted,
    )

    assert code == 3
    assert "completion=completed_unverified" in stream.getvalue()


def test_run_cli_blocked_completion_has_exit_four(tmp_path: Path) -> None:
    scripted = ScriptedClient(
        [
            _scripted_response(
                calls=(_finish_call({"summary": "blocked", "blocked_reason": "input unavailable"}),)
            )
        ]
    )
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "blocked"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None),
        run_client_factory=lambda config: scripted,
    )

    assert code == 4
    assert "completion=blocked" in stream.getvalue()


def test_run_cli_no_change_completion_has_exit_zero(tmp_path: Path) -> None:
    scripted = ScriptedClient(
        [_scripted_response(calls=(_finish_call({"summary": "already satisfied"}),))]
    )
    stream = io.StringIO()

    code = cli.main(
        ["run", "--workspace", str(tmp_path), "inspect"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None),
        run_client_factory=lambda config: scripted,
    )

    assert code == 0
    assert "completion=completed_no_changes" in stream.getvalue()
