"""Offline tests for choosing isolation at every entry point, and for what gets recorded.

The probe is replaced wherever a test needs a particular kernel, so these run the same
on every platform. The one test that needs isolation to really hold is skipped where
Landlock is missing, with the probe's own reason.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest
from rich.console import Console

import proofcoder.cli as cli
import proofcoder.safety.sandbox as sandbox_module
from proofcoder.events import render_sandbox_payload
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
from proofcoder.safety import landlock
from proofcoder.safety.landlock import LandlockProbe
from proofcoder.safety.sandbox import (
    SandboxMode,
    SandboxSettings,
    decide_sandbox,
    sandbox_payload,
)
from proofcoder.tools.command import create_run_command_tool
from proofcoder.trace import list_traces, read_trace
from proofcoder.web.api import ApiRequest, ApiResponse, ApiRouter
from proofcoder.web.runs import BrowserRunManager, BrowserRunStatus

SENSITIVE_SENTINEL = "never-print-this-sandbox-value"
PROBE = landlock.probe()
needs_landlock = pytest.mark.skipif(
    PROBE.abi is None, reason=f"Landlock is not available here: {PROBE.reason}"
)


def _without_landlock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_module,
        "probe_landlock",
        lambda: LandlockProbe(abi=None, reason="disabled at boot"),
    )


def _with_abi(monkeypatch: pytest.MonkeyPatch, abi: int, *, sockets: bool = True) -> None:
    monkeypatch.setattr(sandbox_module, "probe_landlock", lambda: LandlockProbe(abi, None))
    monkeypatch.setattr(sandbox_module, "seccomp_available", lambda: sockets)


def _environ(workspace: Path) -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
        "PATH": os.pathsep.join(
            dict.fromkeys([str(Path(sys.executable).resolve().parent), "/usr/bin", "/bin"])
        ),
        "TMPDIR": str(workspace),
    }


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        content=None,
        reasoning_content=None,
        finish_reason="tool_calls",
        usage=None,
        tool_calls=(
            ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments))),
        ),
    )


def _run(
    workspace: Path, extra: list[str], client: object | None = None
) -> tuple[int, str, ScriptedClient | None]:
    stream = io.StringIO()
    scripted = client

    def factory(config: object) -> object:
        if scripted is None:
            raise AssertionError("the provider must not be contacted")
        return scripted

    code = cli.main(
        ["run", "--workspace", str(workspace), "--no-checkpoint", *extra, "the task"],
        environ=_environ(workspace),
        cwd=workspace,
        console=Console(file=stream, force_terminal=False, color_system=None, width=240),
        run_client_factory=factory,
    )
    return code, stream.getvalue(), scripted if isinstance(scripted, ScriptedClient) else None


def _events(workspace: Path, event_type: str) -> list[dict[str, object]]:
    (summary,) = list_traces(workspace)
    trace = read_trace(workspace, summary.run_id)
    return [dict(event.payload) for event in trace.events if event.event_type.value == event_type]


# ---------------------------------------------------------------- run


def test_required_refuses_before_the_provider_is_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_landlock(monkeypatch)

    code, output, _ = _run(tmp_path, ["--sandbox", "required"])

    assert code == 1
    assert "error_code=SANDBOX_UNAVAILABLE" in output
    assert "disabled at boot" in output or "not Linux" in output
    # Refused before any runtime resource exists: there is no trace to find.
    assert not (tmp_path / ".proofcoder" / "runs").exists()


def test_required_refuses_a_host_that_can_only_isolate_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_abi(monkeypatch, 3, sockets=False)

    code, output, _ = _run(tmp_path, ["--sandbox", "required"])

    assert code == 1
    assert "isolation is partial" in output


def test_auto_without_landlock_runs_as_before_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_landlock(monkeypatch)
    (tmp_path / "ok.py").write_text("print('ran')\n", encoding="utf-8")
    client = ScriptedClient(
        [
            _call("c1", "run_command", {"argv": ["python", "ok.py"]}),
            _call("c2", "finish_task", {"summary": "done"}),
        ]
    )

    code, output, _ = _run(tmp_path, [], client)

    assert code == 0
    assert "(commands run without OS isolation)" in output
    (event,) = _events(tmp_path, "sandbox")
    assert event["mode"] == "auto"
    assert event["status"] in {"unavailable", "unsupported"}
    (result,) = [item for item in _events(tmp_path, "tool_result") if item.get("argv")]
    assert result["sandboxed"] is False
    assert SENSITIVE_SENTINEL not in output


def test_off_is_recorded_as_a_choice_not_as_a_platform_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_abi(monkeypatch, 7)
    client = ScriptedClient([_call("c1", "finish_task", {"summary": "nothing to do"})])

    code, output, _ = _run(tmp_path, ["--sandbox", "off"], client)

    assert code == 0
    (event,) = _events(tmp_path, "sandbox")
    assert event["status"] == "off"
    assert "SANDBOX: status=off mode=off" in output


def test_a_read_path_that_does_not_exist_is_a_configuration_error(tmp_path: Path) -> None:
    code, output, _ = _run(tmp_path, ["--sandbox-read", "no-such-directory"])

    assert code == 1
    assert "error_code=SANDBOX_PATH_INVALID" in output


@needs_landlock
def test_a_run_isolates_its_commands_by_default(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host file", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "peek.py").write_text(
        f"try:\n    open({str(outside)!r}).read()\n    print('read')\n"
        "except OSError:\n    print('denied')\n",
        encoding="utf-8",
    )
    client = ScriptedClient(
        [
            _call("c1", "run_command", {"argv": ["python", "peek.py"]}),
            _call("c2", "finish_task", {"summary": "peeked"}),
        ]
    )

    code, output, scripted = _run(workspace, [], client)

    assert code == 0
    (event,) = _events(workspace, "sandbox")
    assert event["status"] in {"enforced", "partial"}
    (result,) = [item for item in _events(workspace, "tool_result") if item.get("argv")]
    assert result["sandboxed"] is True
    assert "SANDBOX: status=" in output
    # The model is told it is isolated, so a permission error reads as what it is.
    assert scripted is not None
    tools = {tool["function"]["name"]: tool for tool in scripted.requests[0].tools}
    assert "isolated by the operating system" in tools["run_command"]["function"]["description"]


# ---------------------------------------------------------------- doctor, serve, rendering


@pytest.mark.parametrize(
    ("abi", "sockets", "expected"),
    [
        (7, True, "PASS Sandbox: Landlock ABI 7"),
        (3, True, "PASS Sandbox: Landlock ABI 3"),
        (7, False, "WARN Sandbox: Landlock ABI 7"),
        (None, True, "WARN"),
    ],
)
def test_doctor_reports_what_this_host_can_isolate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    abi: int | None,
    sockets: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(
        sandbox_module, "probe_landlock", lambda: LandlockProbe(abi, None if abi else "off")
    )
    monkeypatch.setattr(sandbox_module, "seccomp_available", lambda: sockets)
    stream = io.StringIO()

    code = cli.main(
        ["doctor", "--offline"],
        environ=_environ(tmp_path),
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None, width=240),
    )

    assert code == 0  # a warning, never a failure: auto still runs commands
    line = next(line for line in stream.getvalue().splitlines() if "Sandbox:" in line)
    assert line.startswith(expected)


def test_serve_rejects_a_read_path_that_does_not_exist(tmp_path: Path) -> None:
    stream = io.StringIO()

    code = cli.main(
        ["serve", "--sandbox-read", str(tmp_path / "absent")],
        environ=_environ(tmp_path),
        cwd=tmp_path,
        console=Console(file=stream, force_terminal=False, color_system=None, width=240),
        serve_forever=lambda server: pytest.fail("the server must not start"),
    )

    assert code == 2
    assert "SANDBOX_PATH_INVALID" in stream.getvalue()


def test_rendering_names_the_state_and_warns_when_nothing_is_isolated() -> None:
    enforced = render_sandbox_payload(
        sandbox_payload(
            decide_sandbox(
                SandboxSettings(), probe=lambda: LandlockProbe(7, None), sockets=lambda: True
            )
        )
    )
    missing = render_sandbox_payload(
        sandbox_payload(
            decide_sandbox(SandboxSettings(), probe=lambda: LandlockProbe(None, "disabled"))
        )
    )

    assert enforced == (
        "SANDBOX: status=enforced mode=auto abi=7 tcp=restricted sockets=restricted"
    )
    assert missing.endswith('reason="disabled" (commands run without OS isolation)')


def test_the_tool_description_only_claims_isolation_that_is_in_force(tmp_path: Path) -> None:
    isolated = create_run_command_tool(
        tmp_path,
        sandbox=decide_sandbox(
            SandboxSettings(), probe=lambda: LandlockProbe(7, None), sockets=lambda: True
        ),
    )
    unisolated = create_run_command_tool(
        tmp_path, sandbox=decide_sandbox(SandboxSettings(mode=SandboxMode.OFF))
    )
    network_open = create_run_command_tool(
        tmp_path,
        sandbox=decide_sandbox(
            SandboxSettings(allow_network=True), probe=lambda: LandlockProbe(7, None)
        ),
    )

    assert "TCP connections are refused" in isolated.description
    assert "is not an operating-system sandbox" in unisolated.description
    assert "isolated by the operating system" in network_open.description
    assert "TCP" not in network_open.description


# ---------------------------------------------------------------- browser


def _router(tmp_path: Path, responses: list[ModelResponse]) -> tuple[ApiRouter, BrowserRunManager]:
    scripted = ScriptedClient(responses)
    manager = BrowserRunManager(environ=_environ(tmp_path), client_factory=lambda config: scripted)
    router = ApiRouter(
        sessions=manager,
        environ=_environ(tmp_path),
        cwd=tmp_path,
        default_workspace=tmp_path,
        allow_browse=True,
    )
    return router, manager


def _post(router: ApiRouter, body: dict[str, object]) -> ApiResponse:
    return router.handle(ApiRequest("POST", "/api/runs", {}, body))


def test_the_page_can_only_choose_a_known_mode(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, [])

    response = _post(router, {"workspace": str(tmp_path), "task": "t", "sandbox": "partial"})

    assert response.status == 400
    assert response.body["error"]["code"] == "INVALID_SANDBOX"  # type: ignore[index]
    assert manager.summaries() == ()


def test_a_browser_run_that_requires_isolation_fails_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _without_landlock(monkeypatch)
    router, manager = _router(tmp_path, [])

    response = _post(router, {"workspace": str(tmp_path), "task": "t", "sandbox": "required"})

    assert response.status == 400
    assert response.body["error"]["code"] == "SANDBOX_UNAVAILABLE"  # type: ignore[index]
    # No run exists, so nothing is shown running that was never going to be allowed to.
    assert manager.summaries() == ()


def test_a_browser_run_records_the_mode_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_abi(monkeypatch, 7)
    router, manager = _router(tmp_path, [_call("c1", "finish_task", {"summary": "ok"})])

    started = _post(router, {"workspace": str(tmp_path), "task": "t", "sandbox": "off"})
    run_id = str(started.body["run"]["run_id"])  # type: ignore[index]
    deadline = time.monotonic() + 15
    while manager.get(run_id).summary().status is not BrowserRunStatus.FINISHED:  # type: ignore[union-attr]
        assert time.monotonic() < deadline
        time.sleep(0.01)

    events = router.handle(
        ApiRequest("GET", f"/api/runs/{run_id}/events", {"cursor": "0"}, {})
    ).body["events"]
    (sandbox,) = [event for event in events if event["event_type"] == "sandbox"]  # type: ignore[union-attr,index]
    assert sandbox["payload"]["status"] == "off"  # type: ignore[index]
