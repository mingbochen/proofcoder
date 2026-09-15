"""Offline tests for the `session` command and `run --session`."""

from __future__ import annotations

import io
import json
from pathlib import Path

from rich.console import Console

import proofcoder.cli as cli
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
from proofcoder.session import list_sessions, load_session

SENSITIVE_SENTINEL = "never-print-this-session-value"


def _response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=None,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _finish(call_id: str, summary: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(
            name="finish_task",
            arguments=json.dumps({"summary": summary}),
        ),
    )


def _cli(argv: list[str], *, cwd: Path, client: ScriptedClient | None = None) -> tuple[int, str]:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, color_system=None, width=200)
    code = cli.main(
        argv,
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        cwd=cwd,
        console=console,
        run_client_factory=lambda _config: client,
    )
    return code, stream.getvalue()


def _session_id(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("SESSION "):
            return line.split()[1]
    raise AssertionError(f"no session line in output: {output}")


def test_run_without_session_writes_nothing(tmp_path: Path) -> None:
    client = ScriptedClient([_response(_finish("call-1", "Nothing to do."))])

    code, output = _cli(["run", "--workspace", str(tmp_path), "look"], cwd=tmp_path, client=client)

    assert code == 0
    assert "SESSION:" not in output
    assert list_sessions(tmp_path) == ()
    assert not (tmp_path / ".proofcoder" / "sessions").exists()


def test_run_with_new_session_records_the_run(tmp_path: Path) -> None:
    client = ScriptedClient([_response(_finish("call-1", "Looked around."))])

    code, output = _cli(
        ["run", "--workspace", str(tmp_path), "--session", "new", "look"],
        cwd=tmp_path,
        client=client,
    )

    assert code == 0
    assert "SESSION:" in output
    summaries = list_sessions(tmp_path)
    assert len(summaries) == 1
    stored = load_session(tmp_path, summaries[0].session_id)
    assert len(stored.runs) == 1
    assert stored.runs[0].summary == "Looked around."
    assert stored.runs[0].task == "look"


def test_a_second_run_carries_the_first_without_its_verification(tmp_path: Path) -> None:
    first = ScriptedClient([_response(_finish("call-1", "First pass done."))])
    _, created = _cli(
        ["run", "--workspace", str(tmp_path), "--session", "new", "first task"],
        cwd=tmp_path,
        client=first,
    )
    session_id = list_sessions(tmp_path)[0].session_id

    second = ScriptedClient([_response(_finish("call-2", "Second pass done."))])
    code, _ = _cli(
        ["run", "--workspace", str(tmp_path), "--session", session_id, "second task"],
        cwd=tmp_path,
        client=second,
    )

    assert code == 0
    assert "SESSION:" in created
    prompt = second.requests[0].messages[1]["content"]
    assert "first task" in prompt
    assert "First pass done." in prompt
    assert prompt.endswith("second task")
    assert len(load_session(tmp_path, session_id).runs) == 2


def test_running_an_ended_session_is_refused_before_the_model(tmp_path: Path) -> None:
    first = ScriptedClient([_response(_finish("call-1", "Done."))])
    _cli(
        ["run", "--workspace", str(tmp_path), "--session", "new", "first"],
        cwd=tmp_path,
        client=first,
    )
    session_id = list_sessions(tmp_path)[0].session_id
    _cli(["session", "end", "--workspace", str(tmp_path), session_id], cwd=tmp_path)

    forbidden = ScriptedClient([])
    code, output = _cli(
        ["run", "--workspace", str(tmp_path), "--session", session_id, "second"],
        cwd=tmp_path,
        client=forbidden,
    )

    assert code == 1
    assert "error_code=SESSION_ENDED" in output
    assert list(forbidden.requests) == []


def test_running_an_unknown_session_is_refused(tmp_path: Path) -> None:
    forbidden = ScriptedClient([])

    code, output = _cli(
        ["run", "--workspace", str(tmp_path), "--session", "0" * 32, "task"],
        cwd=tmp_path,
        client=forbidden,
    )

    assert code == 1
    assert "error_code=SESSION_NOT_FOUND" in output
    assert list(forbidden.requests) == []


def test_session_list_show_end_and_delete(tmp_path: Path) -> None:
    client = ScriptedClient([_response(_finish("call-1", "Did the thing."))])
    _cli(
        ["run", "--workspace", str(tmp_path), "--session", "new", "do it"],
        cwd=tmp_path,
        client=client,
    )
    session_id = list_sessions(tmp_path)[0].session_id

    code, listed = _cli(["session", "list", "--workspace", str(tmp_path)], cwd=tmp_path)
    assert code == 0
    assert _session_id(listed) == session_id
    assert "state=open runs=1" in listed

    code, shown = _cli(["session", "show", "--workspace", str(tmp_path), session_id], cwd=tmp_path)
    assert code == 0
    assert "RUN 1" in shown
    assert "finish_task/completed_no_changes" in shown

    code, ended = _cli(["session", "end", "--workspace", str(tmp_path), session_id], cwd=tmp_path)
    assert code == 0
    assert "ended=" in ended

    code, deleted = _cli(
        ["session", "delete", "--workspace", str(tmp_path), session_id], cwd=tmp_path
    )
    assert code == 0
    assert "deleted" in deleted
    assert list_sessions(tmp_path) == ()


def test_session_show_reports_a_carried_verification_as_expired(tmp_path: Path) -> None:
    (tmp_path / "helper.py").write_text("value = 1\n", encoding="utf-8")
    client = ScriptedClient(
        [
            _response(
                ToolCall(
                    id="call-1",
                    function=FunctionCall(
                        name="run_command",
                        arguments=json.dumps(
                            {"argv": ["python", "-c", "pass"], "timeout_seconds": 30}
                        ),
                    ),
                )
            ),
            _response(_finish("call-2", "Ran a check.")),
        ]
    )
    _cli(
        ["run", "--workspace", str(tmp_path), "--session", "new", "check"],
        cwd=tmp_path,
        client=client,
    )
    session_id = list_sessions(tmp_path)[0].session_id

    code, shown = _cli(["session", "show", "--workspace", str(tmp_path), session_id], cwd=tmp_path)

    assert code == 0
    # Whether or not the command counted as evidence, a listing never prints a bare
    # exit code that could read as evidence this workspace still holds.
    if "verification" in shown:
        assert "verification(expired)" in shown


def test_session_commands_refuse_a_missing_workspace(tmp_path: Path) -> None:
    code, output = _cli(["session", "list", "--workspace", str(tmp_path / "absent")], cwd=tmp_path)

    assert code == 2
    assert "INVALID_WORKSPACE" in output


def test_session_show_reports_an_invalid_identifier(tmp_path: Path) -> None:
    code, output = _cli(["session", "show", "--workspace", str(tmp_path), "nope"], cwd=tmp_path)

    assert code == 2
    assert "INVALID_SESSION_ID" in output
