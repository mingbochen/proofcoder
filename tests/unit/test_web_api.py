"""Offline tests for the transport-independent web API.

The router is exercised without binding a socket, so every status, workspace,
run, and trace path is covered by plain function calls with deterministic input.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from proofcoder.agent_runtime import AgentRunLimits
from proofcoder.config import ProofCoderConfig
from proofcoder.context import DEFAULT_CONTEXT_BUDGET_BYTES
from proofcoder.errors import DeepSeekAPIError
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
from proofcoder.web.api import (
    MAX_HISTORY_ENTRIES,
    ApiRequest,
    ApiResponse,
    ApiRouter,
)
from proofcoder.web.sessions import SessionManager, SessionStatus

SENSITIVE_SENTINEL = "never-echo-this-api-value"
REASONING = "hidden-reasoning-must-stay-private"
ENVIRON = {"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL}


class _SuccessClient:
    def check_connection(self) -> ModelResponse:
        return ModelResponse(
            content="ignored",
            reasoning_content=REASONING,
            finish_reason="stop",
            usage=None,
        )


class _FailureClient:
    def check_connection(self) -> ModelResponse:
        raise DeepSeekAPIError(f"unsafe detail: {SENSITIVE_SENTINEL}")


def _response(*, content: str | None = None, calls: tuple[ToolCall, ...] = ()) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=REASONING,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments)))


def _router(
    tmp_path: Path,
    *,
    responses: list[ModelResponse] | None = None,
    environ: Mapping[str, str] | None = None,
    connectivity: object | None = None,
    allow_browse: bool = True,
) -> tuple[ApiRouter, SessionManager]:
    scripted = ScriptedClient(responses or [])
    manager = SessionManager(
        environ=ENVIRON if environ is None else environ,
        client_factory=lambda config: scripted,
    )
    kwargs: dict[str, object] = {
        "sessions": manager,
        "environ": ENVIRON if environ is None else environ,
        "cwd": tmp_path,
        "default_workspace": tmp_path,
        "allow_browse": allow_browse,
    }
    if connectivity is not None:
        kwargs["connectivity_factory"] = connectivity
    return ApiRouter(**kwargs), manager  # type: ignore[arg-type]


def _get(router: ApiRouter, path: str, query: Mapping[str, str] | None = None) -> ApiResponse:
    return router.handle(ApiRequest("GET", path, dict(query or {}), {}))


def _post(router: ApiRouter, path: str, body: Mapping[str, object] | None = None) -> ApiResponse:
    return router.handle(ApiRequest("POST", path, {}, dict(body or {})))


def _finish_run(manager: SessionManager, run_id: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = manager.get(run_id)
        assert session is not None
        if session.summary().status is SessionStatus.FINISHED:
            return
        time.sleep(0.01)
    raise AssertionError("the run did not finish within the test timeout")


# ---------- routing ----------


def test_unknown_paths_and_methods_are_rejected(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    assert _get(router, "/nope").status == 404
    assert _get(router, "/api/nope").status == 404
    assert _post(router, "/api/status").status == 404
    assert _get(router, "/api/runs/abc/unknown").status == 404
    assert _get(router, "/").body["error"]["code"] == "NOT_FOUND"  # type: ignore[index]


# ---------- status and doctor ----------


def test_status_reports_configuration_without_the_credential(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    response = _get(router, "/api/status")
    body = response.body

    assert response.status == 200
    assert body["model"] == "deepseek-v4-flash"
    assert body["api_key_configured"] is True
    assert body["configuration_error"] is None
    assert body["default_workspace"] == str(tmp_path)
    assert body["limits"]["max_steps"]["maximum"] == 64  # type: ignore[index]
    assert SENSITIVE_SENTINEL not in json.dumps(body)


def test_status_reports_a_missing_credential_as_a_boolean(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, environ={})

    body = _get(router, "/api/status").body

    assert body["api_key_configured"] is False
    assert body["model"] == "deepseek-v4-flash"


def test_status_reports_an_invalid_reasoning_effort(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, environ={"DEEPSEEK_REASONING_EFFORT": "unsupported"})

    body = _get(router, "/api/status").body

    assert body["model"] is None
    assert "DEEPSEEK_REASONING_EFFORT" in str(body["configuration_error"])


def test_offline_doctor_skips_the_provider(tmp_path: Path) -> None:
    def forbidden(config: ProofCoderConfig) -> object:
        raise AssertionError("offline doctor must not build a client")

    router, _ = _router(tmp_path, connectivity=forbidden)

    body = _post(router, "/api/doctor", {"offline": True}).body

    assert body["ok"] is True
    assert body["offline"] is True
    assert body["checks"][-1]["detail"] == "skipped in offline mode"  # type: ignore[index]


def test_online_doctor_reports_a_successful_connection(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, connectivity=lambda config: _SuccessClient())

    body = _post(router, "/api/doctor", {"offline": False}).body

    assert body["ok"] is True
    assert body["checks"][-1]["ok"] is True  # type: ignore[index]
    assert REASONING not in json.dumps(body)


def test_online_doctor_hides_provider_failure_detail(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, connectivity=lambda config: _FailureClient())

    body = _post(router, "/api/doctor", {"offline": False}).body

    assert body["ok"] is False
    assert SENSITIVE_SENTINEL not in json.dumps(body)


def test_online_doctor_reports_missing_configuration(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, environ={}, connectivity=lambda config: _SuccessClient())

    body = _post(router, "/api/doctor", {}).body

    assert body["ok"] is False
    assert "DEEPSEEK_API_KEY" in str(body["checks"][-1]["detail"])  # type: ignore[index]


def test_doctor_reports_an_unwritable_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import proofcoder.web.api as api_module

    monkeypatch.setattr(api_module.os, "access", lambda path, mode: False)
    router, _ = _router(tmp_path)

    body = _post(router, "/api/doctor", {"offline": True}).body

    assert body["ok"] is False
    assert body["checks"][2]["ok"] is False  # type: ignore[index]


# ---------- workspace ----------


def test_workspace_resolution_reports_existence_and_run_count(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")])
    router.handle(ApiRequest("POST", "/api/runs", {}, {"workspace": str(tmp_path), "task": "one"}))
    run_id = manager.summaries()[0].run_id
    _finish_run(manager, run_id)

    body = _post(router, "/api/workspace", {"path": str(tmp_path)}).body

    assert body["path"] == str(tmp_path)
    assert body["exists"] is True
    assert body["is_directory"] is True
    assert body["run_count"] == 1


def test_workspace_resolution_accepts_a_relative_path(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    router, _ = _router(tmp_path)

    body = _post(router, "/api/workspace", {"path": "child"}).body

    assert body["path"] == str(tmp_path / "child")
    assert body["name"] == "child"


def test_workspace_resolution_reports_a_missing_directory(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    body = _post(router, "/api/workspace", {"path": str(tmp_path / "absent")}).body

    assert body["exists"] is False
    assert body["is_directory"] is False
    assert body["run_count"] == 0


def test_workspace_resolution_requires_a_path(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    response = _post(router, "/api/workspace", {"path": "  "})

    assert response.status == 400
    assert response.body["error"]["code"] == "INVALID_WORKSPACE"  # type: ignore[index]


def test_browsing_lists_directories_and_hides_runtime_caches(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    router, _ = _router(tmp_path)

    body = _get(router, "/api/workspace/browse", {"path": str(tmp_path)}).body

    assert [item["name"] for item in body["directories"]] == ["alpha", "beta"]  # type: ignore[index]
    assert body["parent"] == str(tmp_path.parent)
    assert body["truncated"] is False
    assert body["roots"]


def test_browsing_defaults_to_the_configured_workspace(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    body = _get(router, "/api/workspace/browse").body

    assert body["path"] == str(tmp_path)


def test_browsing_rejects_a_non_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    router, _ = _router(tmp_path)

    response = _get(router, "/api/workspace/browse", {"path": str(target)})

    assert response.status == 404
    assert response.body["error"]["code"] == "NOT_A_DIRECTORY"  # type: ignore[index]


def test_browsing_can_be_disabled(tmp_path: Path) -> None:
    router, _ = _router(tmp_path, allow_browse=False)

    response = _get(router, "/api/workspace/browse", {"path": str(tmp_path)})

    assert response.status == 403
    assert _get(router, "/api/status").body["browse_enabled"] is False


def test_browsing_reports_a_denied_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(self: Path) -> object:
        raise OSError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    router, _ = _router(tmp_path)

    response = _get(router, "/api/workspace/browse", {"path": str(tmp_path)})

    assert response.status == 403
    assert response.body["error"]["code"] == "BROWSE_DENIED"  # type: ignore[index]


# ---------- runs ----------


def test_starting_a_run_validates_the_request(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    assert _post(router, "/api/runs", {"workspace": str(tmp_path)}).status == 400
    assert _post(router, "/api/runs", {"task": "no workspace"}).status == 400
    assert (
        _post(router, "/api/runs", {"workspace": str(tmp_path / "absent"), "task": "x"}).status
        == 400
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_steps", 0),
        ("max_steps", 65),
        ("max_steps", "many"),
        ("max_steps", True),
        ("max_steps", 1.5),
        ("max_seconds", 0),
        ("max_seconds", "slow"),
        ("context_budget_bytes", 1),
        ("max_consecutive_failures", 0),
        ("max_api_attempts", 4),
    ],
)
def test_out_of_range_limits_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    router, _ = _router(tmp_path)

    response = _post(
        router,
        "/api/runs",
        {"workspace": str(tmp_path), "task": "bounded", field: value},
    )

    assert response.status == 400
    assert response.body["error"]["code"] == "INVALID_LIMIT"  # type: ignore[index]


def test_limits_fall_back_to_defaults(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")])

    response = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "defaults"})
    run_id = str(response.body["run"]["run_id"])  # type: ignore[index]
    session = manager.get(run_id)
    assert session is not None
    _finish_run(manager, run_id)

    assert response.status == 201
    assert session.limits == AgentRunLimits(
        max_steps=8,
        max_seconds=600.0,
        context_budget_bytes=DEFAULT_CONTEXT_BUDGET_BYTES,
        max_consecutive_failures=5,
        max_api_attempts=3,
    )


def test_explicit_limits_are_applied(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")])

    response = _post(
        router,
        "/api/runs",
        {
            "workspace": str(tmp_path),
            "task": "tight",
            "max_steps": 1,
            "max_seconds": 30,
            "context_budget_bytes": 8192,
            "max_consecutive_failures": 2,
            "max_api_attempts": 1,
        },
    )
    run_id = str(response.body["run"]["run_id"])  # type: ignore[index]
    session = manager.get(run_id)
    assert session is not None
    _finish_run(manager, run_id)

    assert session.limits.max_steps == 1
    assert session.limits.max_seconds == 30.0
    assert session.limits.context_budget_bytes == 8192
    assert session.limits.max_api_attempts == 1


def test_run_events_are_paged_by_cursor(tmp_path: Path) -> None:
    router, manager = _router(
        tmp_path,
        responses=[
            _response(
                content="creating",
                calls=(
                    _call("create-1", "create_file", {"path": "made.py", "content": "x = 1\n"}),
                ),
            ),
            _response(
                content="finishing",
                calls=(
                    _call(
                        "finish-1",
                        "finish_task",
                        {"summary": "made it", "changed_files": ["made.py"]},
                    ),
                ),
            ),
        ],
    )
    started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "create"})
    run_id = str(started.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, run_id)

    first = _get(router, f"/api/runs/{run_id}/events", {"cursor": "0"}).body
    tail = _get(router, f"/api/runs/{run_id}/events", {"cursor": str(first["cursor"])}).body

    assert first["done"] is True
    assert tail["events"] == []
    assert tail["cursor"] == first["cursor"]
    assert SENSITIVE_SENTINEL not in json.dumps(first)
    assert REASONING not in json.dumps(first)


def test_run_events_accept_a_bounded_wait(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")])
    started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "wait"})
    run_id = str(started.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, run_id)

    body = _get(router, f"/api/runs/{run_id}/events", {"cursor": "0", "wait": "1"}).body

    assert body["done"] is True
    assert body["events"]


@pytest.mark.parametrize(
    "query",
    [{"cursor": "-1"}, {"cursor": "many"}, {"wait": "600"}, {"wait": "soon"}],
)
def test_invalid_event_queries_are_rejected(tmp_path: Path, query: dict[str, str]) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")])
    started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "query"})
    run_id = str(started.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, run_id)

    response = _get(router, f"/api/runs/{run_id}/events", query)

    assert response.status == 400
    assert response.body["error"]["code"] == "INVALID_QUERY"  # type: ignore[index]


def test_unknown_runs_are_reported(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)
    missing = "a" * 32

    assert _get(router, f"/api/runs/{missing}").status == 404
    assert _get(router, f"/api/runs/{missing}/events").status == 404
    assert _post(router, f"/api/runs/{missing}/cancel").status == 404


def test_listing_and_cancelling_runs(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")])
    started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "list me"})
    run_id = str(started.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, run_id)

    listed = _get(router, "/api/runs").body
    detail = _get(router, f"/api/runs/{run_id}").body
    cancelled = _post(router, f"/api/runs/{run_id}/cancel").body

    assert [item["run_id"] for item in listed["runs"]] == [run_id]  # type: ignore[index]
    assert detail["run"]["task"] == "list me"  # type: ignore[index]
    assert cancelled["cancelled"] is False


# ---------- traces ----------


def test_traces_are_listed_newest_first_with_their_task(tmp_path: Path) -> None:
    # A text-only response triggers one protocol repair, so each run consumes two.
    router, manager = _router(tmp_path, responses=[_response(content="stop")] * 4)
    first = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "older"})
    first_id = str(first.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, first_id)
    time.sleep(0.01)
    second = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "newer"})
    second_id = str(second.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, second_id)

    body = _get(router, "/api/traces", {"workspace": str(tmp_path)}).body

    assert body["total"] == 2
    tasks = [item["task"] for item in body["traces"]]  # type: ignore[index]
    assert tasks == ["newer", "older"]
    assert body["traces"][0]["termination_reason"] == "model_stopped"  # type: ignore[index]


def test_trace_detail_replays_stored_events(tmp_path: Path) -> None:
    router, manager = _router(
        tmp_path,
        responses=[
            _response(
                content="creating",
                calls=(
                    _call("create-1", "create_file", {"path": "made.py", "content": "x = 1\n"}),
                ),
            ),
            _response(
                content="finishing",
                calls=(
                    _call(
                        "finish-1",
                        "finish_task",
                        {"summary": "made it", "changed_files": ["made.py"]},
                    ),
                ),
            ),
        ],
    )
    started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": "replay me"})
    run_id = str(started.body["run"]["run_id"])  # type: ignore[index]
    _finish_run(manager, run_id)

    body = _get(router, f"/api/traces/{run_id}", {"workspace": str(tmp_path)}).body

    assert body["run_id"] == run_id
    assert body["task"] == "replay me"
    assert body["trace_complete"] is True
    assert body["completion_status"] == "completed_unverified"
    assert body["changed_files"] == ["made.py"]
    assert body["issues"] == []
    assert "model_calls=2" in str(body["report"])
    assert REASONING not in json.dumps(body)


def test_trace_requests_validate_their_workspace_and_run_id(tmp_path: Path) -> None:
    router, _ = _router(tmp_path)

    assert _get(router, "/api/traces").status == 400
    assert _get(router, "/api/traces", {"workspace": str(tmp_path / "absent")}).status == 404
    assert _get(router, "/api/traces/not-a-run-id", {"workspace": str(tmp_path)}).status == 404
    assert _get(router, "/api/traces/" + "b" * 32, {"workspace": str(tmp_path)}).status == 404


@pytest.mark.parametrize("limit", ["0", str(MAX_HISTORY_ENTRIES + 1), "many"])
def test_invalid_history_limits_are_rejected(tmp_path: Path, limit: str) -> None:
    router, _ = _router(tmp_path)

    response = _get(router, "/api/traces", {"workspace": str(tmp_path), "limit": limit})

    assert response.status == 400
    assert response.body["error"]["code"] == "INVALID_LIMIT"  # type: ignore[index]


def test_history_limit_bounds_the_returned_entries(tmp_path: Path) -> None:
    router, manager = _router(tmp_path, responses=[_response(content="stop")] * 4)
    for index in range(2):
        started = _post(router, "/api/runs", {"workspace": str(tmp_path), "task": f"run {index}"})
        _finish_run(manager, str(started.body["run"]["run_id"]))  # type: ignore[index]

    body = _get(router, "/api/traces", {"workspace": str(tmp_path), "limit": "1"}).body

    assert body["total"] == 2
    assert len(body["traces"]) == 1  # type: ignore[arg-type]


def test_unreadable_trace_directories_still_list(tmp_path: Path) -> None:
    runs = tmp_path / ".proofcoder" / "runs" / ("c" * 32)
    runs.mkdir(parents=True)
    router, _ = _router(tmp_path)

    body = _get(router, "/api/traces", {"workspace": str(tmp_path)}).body

    assert body["total"] == 1
    assert body["traces"][0]["task"] == ""  # type: ignore[index]
    assert body["traces"][0]["status"] == "incomplete"  # type: ignore[index]
