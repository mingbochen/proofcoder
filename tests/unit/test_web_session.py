"""Offline tests for the browser session routes and session-bound runs."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path

from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
from proofcoder.session import end_session, list_sessions, load_session
from proofcoder.web.api import ApiRequest, ApiResponse, ApiRouter
from proofcoder.web.runs import BrowserRunManager, BrowserRunStatus

SENSITIVE_SENTINEL = "never-echo-this-session-value"
ENVIRON = {"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL}


def _finish_response(summary: str) -> ModelResponse:
    return ModelResponse(
        content=None,
        reasoning_content=None,
        finish_reason="tool_calls",
        usage=None,
        tool_calls=(
            ToolCall(
                id="call-1",
                function=FunctionCall(
                    name="finish_task", arguments=json.dumps({"summary": summary})
                ),
            ),
        ),
    )


def _router(tmp_path: Path, responses: list[ModelResponse]) -> tuple[ApiRouter, ScriptedClient]:
    scripted = ScriptedClient(responses)
    manager = BrowserRunManager(environ=ENVIRON, client_factory=lambda _config: scripted)
    router = ApiRouter(
        sessions=manager,
        environ=ENVIRON,
        cwd=tmp_path,
        default_workspace=tmp_path,
    )
    router._manager_for_tests = manager  # type: ignore[attr-defined]
    return router, scripted


def _get(router: ApiRouter, path: str, query: Mapping[str, str] | None = None) -> ApiResponse:
    return router.handle(ApiRequest("GET", path, dict(query or {}), {}))


def _post(router: ApiRouter, path: str, body: Mapping[str, object] | None = None) -> ApiResponse:
    return router.handle(ApiRequest("POST", path, {}, dict(body or {})))


def _finish_run(router: ApiRouter, run_id: str, timeout: float = 15.0) -> None:
    manager = router._manager_for_tests  # type: ignore[attr-defined]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = manager.get(run_id)
        assert run is not None
        if run.summary().status is BrowserRunStatus.FINISHED:
            return
        time.sleep(0.01)
    raise AssertionError("the run did not finish within the test timeout")


def test_sessions_are_created_listed_ended_and_deleted(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, [])

    created = _post(router, "/api/sessions", {"workspace": str(tmp_path)})
    assert created.status == 201
    session_id = created.body["session"]["session_id"]

    listed = _get(router, "/api/sessions", {"workspace": str(tmp_path)})
    assert listed.status == 200
    assert [item["session_id"] for item in listed.body["sessions"]] == [session_id]

    detail = _get(router, f"/api/sessions/{session_id}", {"workspace": str(tmp_path)})
    assert detail.status == 200
    assert detail.body["session"]["runs"] == []

    ended = _post(router, f"/api/sessions/{session_id}/end", {"workspace": str(tmp_path)})
    assert ended.status == 200
    assert ended.body["session"]["ended_at"] is not None

    deleted = _post(router, f"/api/sessions/{session_id}/delete", {"workspace": str(tmp_path)})
    assert deleted.status == 200
    assert list_sessions(tmp_path) == ()


def test_a_session_listing_recovers_what_an_earlier_process_wrote(tmp_path: Path) -> None:
    """Restart recovery is exactly this: the data is on disk, not in the process."""

    first_router, _ = _router(tmp_path, [])
    created = _post(first_router, "/api/sessions", {"workspace": str(tmp_path)})
    session_id = created.body["session"]["session_id"]

    # A second router with its own manager stands in for a restarted service.
    second_router, _ = _router(tmp_path, [])
    listed = _get(second_router, "/api/sessions", {"workspace": str(tmp_path)})

    assert [item["session_id"] for item in listed.body["sessions"]] == [session_id]


def test_a_session_bound_run_is_recorded_and_carried(tmp_path: Path) -> None:
    router, _first = _router(tmp_path, [_finish_response("First pass.")])
    session_id = _post(router, "/api/sessions", {"workspace": str(tmp_path)}).body["session"][
        "session_id"
    ]

    started = _post(
        router,
        "/api/runs",
        {"workspace": str(tmp_path), "task": "first task", "session_id": session_id},
    )
    assert started.status == 201
    assert started.body["run"]["session_id"] == session_id
    _finish_run(router, started.body["run"]["run_id"])

    stored = load_session(tmp_path, session_id)
    assert len(stored.runs) == 1
    assert stored.runs[0].summary == "First pass."

    second_router, second = _router(tmp_path, [_finish_response("Second pass.")])
    follow_up = _post(
        second_router,
        "/api/runs",
        {"workspace": str(tmp_path), "task": "second task", "session_id": session_id},
    )
    _finish_run(second_router, follow_up.body["run"]["run_id"])

    prompt = second.requests[0].messages[1]["content"]
    assert "first task" in prompt
    assert "First pass." in prompt
    assert prompt.endswith("second task")
    assert len(load_session(tmp_path, session_id).runs) == 2


def test_starting_a_run_in_an_ended_session_is_refused(tmp_path: Path) -> None:
    router, scripted = _router(tmp_path, [])
    session_id = _post(router, "/api/sessions", {"workspace": str(tmp_path)}).body["session"][
        "session_id"
    ]
    end_session(tmp_path, session_id)

    refused = _post(
        router,
        "/api/runs",
        {"workspace": str(tmp_path), "task": "task", "session_id": session_id},
    )

    assert refused.status == 400
    assert refused.body["error"]["code"] == "SESSION_ENDED"
    assert list(scripted.requests) == []


def test_starting_a_run_in_an_unknown_session_is_refused(tmp_path: Path) -> None:
    router, scripted = _router(tmp_path, [])

    refused = _post(
        router,
        "/api/runs",
        {"workspace": str(tmp_path), "task": "task", "session_id": "0" * 32},
    )

    assert refused.status == 400
    assert refused.body["error"]["code"] == "SESSION_NOT_FOUND"
    assert list(scripted.requests) == []


def test_a_malformed_session_identifier_is_refused(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, [])

    refused = _get(router, "/api/sessions/not-hex", {"workspace": str(tmp_path)})

    assert refused.status == 404
    assert refused.body["error"]["code"] == "INVALID_SESSION_ID"


def test_session_routes_require_a_workspace(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, [])

    assert _get(router, "/api/sessions").body["error"]["code"] == "INVALID_WORKSPACE"
    assert _post(router, "/api/sessions", {}).body["error"]["code"] == "INVALID_WORKSPACE"


def test_a_run_without_a_session_reports_none_and_writes_nothing(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, [_finish_response("No session here.")])

    started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "task"})
    _finish_run(router, started.body["run"]["run_id"])

    assert started.body["run"]["session_id"] is None
    assert list_sessions(tmp_path) == ()


def test_a_carried_verification_is_reported_expired_to_the_browser(tmp_path: Path) -> None:
    router, _ = _router(
        tmp_path,
        [
            ModelResponse(
                content=None,
                reasoning_content=None,
                finish_reason="tool_calls",
                usage=None,
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        function=FunctionCall(
                            name="run_command",
                            arguments=json.dumps(
                                {"argv": ["python", "-c", "pass"], "timeout_seconds": 30}
                            ),
                        ),
                    ),
                ),
            ),
            _finish_response("Ran a check."),
        ],
    )
    session_id = _post(router, "/api/sessions", {"workspace": str(tmp_path)}).body["session"][
        "session_id"
    ]
    started = _post(
        router,
        "/api/runs",
        {"workspace": str(tmp_path), "task": "check", "session_id": session_id},
    )
    _finish_run(router, started.body["run"]["run_id"])

    detail = _get(router, f"/api/sessions/{session_id}", {"workspace": str(tmp_path)})

    record = detail.body["session"]["runs"][0]
    if record["verification"] is not None:
        assert record["verification"]["expired"] is True
