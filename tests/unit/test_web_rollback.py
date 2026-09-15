"""Stage F.4: the browser rollback surface and the approval it is bound to."""

from __future__ import annotations

from pathlib import Path

import pytest

from proofcoder.checkpoint import create_checkpoint
from proofcoder.rollback import build_rollback_plan, plan_digest
from proofcoder.web.api import ApiRequest, ApiResponse, ApiRouter
from proofcoder.web.runs import BrowserRunManager, BrowserRunStatus

TARGET_RUN = "a" * 32
# Written into a workspace .env so the tests can prove no response surface shows it.
SENSITIVE_SENTINEL = "never-echo-this-value"
ENVIRON = {"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL}


def _router(sessions: BrowserRunManager | None = None) -> ApiRouter:
    manager = sessions or BrowserRunManager(environ=ENVIRON, client_factory=lambda config: None)
    return ApiRouter(sessions=manager, environ=ENVIRON)


def _plan(router: ApiRouter, workspace: Path, run_id: str = TARGET_RUN) -> ApiResponse:
    return router.handle(
        ApiRequest("GET", f"/api/checkpoints/{run_id}/plan", {"workspace": str(workspace)}, {})
    )


def _apply(
    router: ApiRouter,
    workspace: Path,
    digest: object,
    run_id: str = TARGET_RUN,
) -> ApiResponse:
    body: dict[str, object] = {"workspace": str(workspace)}
    if digest is not None:
        body["plan_digest"] = digest
    return router.handle(ApiRequest("POST", f"/api/checkpoints/{run_id}/rollback", {}, body))


def _workspace(root: Path) -> Path:
    (root / "pkg").mkdir()
    (root / "pkg" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "gone.txt").write_text("gone\n", encoding="utf-8")
    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}\n", encoding="utf-8")
    create_checkpoint(root, TARGET_RUN)
    return root


def _change(root: Path) -> None:
    (root / "pkg" / "app.py").write_text("value = 2\n", encoding="utf-8")
    (root / "gone.txt").unlink()
    (root / "build").mkdir()
    (root / "build" / "out.o").write_text("object\n", encoding="utf-8")
    (root / ".env").write_text(f"TOKEN_NAME={SENSITIVE_SENTINEL}-rotated\n", encoding="utf-8")


def test_plan_describes_every_action_and_gap_without_writing(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    router = _router()

    response = _plan(router, tmp_path)

    assert response.status == 200
    body = response.body
    assert body["target_run_id"] == TARGET_RUN
    assert body["empty"] is False
    actions = {(item["action"], item["path"]) for item in body["items"]}
    assert ("restore", "pkg/app.py") in actions
    assert ("recreate", "gone.txt") in actions
    assert ("delete", "build/out.o") in actions
    assert ("remove_directory", "build") in actions
    assert body["skipped"] == [{"path": ".env", "reason": "sensitive_changed"}]
    assert len(str(body["plan_digest"])) == 64
    # Asking for a plan changes nothing.
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert SENSITIVE_SENTINEL not in str(body)


def test_an_approval_only_applies_to_the_plan_it_was_given_for(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    router = _router()
    digest = _plan(router, tmp_path).body["plan_digest"]

    # The workspace moves after the plan was shown, so that approval no longer holds.
    (tmp_path / "late.txt").write_text("written after the plan\n", encoding="utf-8")
    response = _apply(router, tmp_path, digest)

    assert response.status == 409
    assert response.body["error"]["code"] == "PLAN_CHANGED"
    # The replacement plan is returned so the caller can review and confirm that one.
    assert any(item["path"] == "late.txt" for item in response.body["items"])
    assert response.body["plan_digest"] != digest
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "late.txt").is_file()


def test_confirming_the_current_plan_applies_it(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    router = _router()
    digest = _plan(router, tmp_path).body["plan_digest"]

    response = _apply(router, tmp_path, digest)

    assert response.status == 200
    body = response.body
    assert body["complete"] is True
    assert body["target_run_id"] == TARGET_RUN
    assert body["run_id"] != TARGET_RUN
    assert body["restored"] == ["pkg/app.py"]
    assert body["recreated"] == ["gone.txt"]
    assert body["deleted"] == ["build/out.o"]
    assert body["skipped"] == [{"path": ".env", "reason": "sensitive_changed"}]
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "gone.txt").read_text(encoding="utf-8") == "gone\n"
    assert not (tmp_path / "build").exists()
    # The credential file is reported and left exactly as the run left it.
    assert (tmp_path / ".env").read_text(encoding="utf-8").endswith("-rotated\n")
    assert SENSITIVE_SENTINEL not in str(body)


@pytest.mark.parametrize("digest", [None, "", 42, "0" * 64])
def test_a_missing_or_wrong_digest_never_writes(tmp_path: Path, digest: object) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    router = _router()

    response = _apply(router, tmp_path, digest)

    assert response.status in {400, 409}
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (tmp_path / "gone.txt").exists()


def test_a_live_run_blocks_rolling_its_workspace_back(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    class _BusyManager(BrowserRunManager):
        def workspace_busy(self, workspace: Path) -> bool:
            return True

    router = _router(_BusyManager(environ=ENVIRON, client_factory=lambda config: None))
    digest = _plan(router, tmp_path).body["plan_digest"]
    response = _apply(router, tmp_path, digest)

    assert response.status == 409
    assert response.body["error"]["code"] == "WORKSPACE_BUSY"
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_workspace_busy_tracks_only_running_sessions(tmp_path: Path) -> None:
    manager = BrowserRunManager(environ=ENVIRON, client_factory=lambda config: None)

    assert manager.workspace_busy(tmp_path) is False

    session = manager.start(
        workspace=tmp_path,
        task="anything",
        limits=__import__("proofcoder.agent_runtime", fromlist=["AgentRunLimits"]).AgentRunLimits(),
    )
    for _ in range(200):
        if session.status is not BrowserRunStatus.RUNNING:
            break
        __import__("time").sleep(0.02)
    manager.shutdown(timeout=10.0)

    assert manager.workspace_busy(tmp_path) is False
    assert manager.workspace_busy(tmp_path / "elsewhere") is False


def test_a_run_without_a_checkpoint_is_a_clear_not_found(tmp_path: Path) -> None:
    router = _router()

    response = _plan(router, tmp_path)

    assert response.status == 404
    assert response.body["error"]["code"] == "CHECKPOINT_NOT_FOUND"


def test_an_invalid_run_identifier_is_refused(tmp_path: Path) -> None:
    router = _router()

    response = _plan(router, tmp_path, run_id="not-a-run-id")

    assert response.status == 404
    assert response.body["error"]["code"] == "INVALID_RUN_ID"


def test_the_workspace_must_be_supplied_and_real(tmp_path: Path) -> None:
    router = _router()

    missing = router.handle(ApiRequest("GET", f"/api/checkpoints/{TARGET_RUN}/plan", {}, {}))
    absent = _apply(router, tmp_path / "missing", "0" * 64)
    blank = router.handle(
        ApiRequest("POST", f"/api/checkpoints/{TARGET_RUN}/rollback", {}, {"plan_digest": "0" * 64})
    )

    assert missing.status == 400
    assert absent.status == 404
    assert blank.status == 400
    assert blank.body["error"]["code"] == "INVALID_WORKSPACE"


def test_unsupported_methods_on_the_rollback_routes_are_not_found(tmp_path: Path) -> None:
    router = _router()

    posted_plan = router.handle(ApiRequest("POST", f"/api/checkpoints/{TARGET_RUN}/plan", {}, {}))
    fetched_rollback = router.handle(
        ApiRequest("GET", f"/api/checkpoints/{TARGET_RUN}/rollback", {}, {})
    )

    assert posted_plan.status == 404
    assert fetched_rollback.status == 404


def test_the_digest_changes_with_the_plan_and_not_with_the_run(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)

    first = plan_digest(build_rollback_plan(tmp_path, TARGET_RUN))
    again = plan_digest(build_rollback_plan(tmp_path, TARGET_RUN))
    (tmp_path / "late.txt").write_text("later\n", encoding="utf-8")
    after = plan_digest(build_rollback_plan(tmp_path, TARGET_RUN))

    # Stable for the same plan, different once the plan itself differs.
    assert first == again
    assert after != first


def test_a_rollback_trace_is_listed_by_the_run_it_undid(tmp_path: Path) -> None:
    _workspace(tmp_path)
    _change(tmp_path)
    router = _router()
    digest = _plan(router, tmp_path).body["plan_digest"]
    applied = _apply(router, tmp_path, digest)

    listing = router.handle(ApiRequest("GET", "/api/traces", {"workspace": str(tmp_path)}, {}))
    entries = {entry["run_id"]: entry for entry in listing.body["traces"]}
    operation = entries[applied.body["run_id"]]

    assert operation["termination_reason"] == "rollback"
    assert operation["target_run_id"] == TARGET_RUN
    assert operation["task"] == ""
