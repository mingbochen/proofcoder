"""Offline tests for the command-line and browser approval entry points."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

import proofcoder.safety.commands as command_policy
from proofcoder.approval import ApprovalMode, ApprovalOutcome, ApprovalRequest
from proofcoder.cli import main
from proofcoder.safety.policy import POLICY_FILENAME
from proofcoder.web.api import ApiRequest, ApiRouter
from proofcoder.web.runs import BrowserRun, BrowserRunManager, BrowserRunStatus

SENSITIVE_SENTINEL = "never-a-real-approval-credential"
POLICY_BODY = """
schema_version = 1

[[command]]
executable = "make"
subcommands = ["test"]
decision = "allow"
kind = "test"
"""


@pytest.fixture(autouse=True)
def _stable_executable_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        command_policy.shutil,
        "which",
        lambda executable, *, path: str(Path(sys.executable).resolve()),
    )


def _environment() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
        "PATH": str(Path(sys.executable).resolve().parent),
    }


def _request(digest_seed: str = "wip") -> ApprovalRequest:
    return ApprovalRequest(
        request_id="request-1",
        display_argv=("git", "commit", "-m", digest_seed),
        relative_cwd=".",
        timeout_seconds=60,
        command_kind="git_write",
        decision_source="builtin",
        approval_timeout_seconds=300,
    )


def _session() -> BrowserRun:
    from proofcoder.agent_runtime import AgentRunLimits

    return BrowserRun(
        run_id="a" * 32,
        workspace=Path.cwd(),
        task="commit the change",
        limits=AgentRunLimits(),
        started_at="2026-09-15T00:00:00Z",
    )


# --- command line --------------------------------------------------------


def test_an_unloaded_policy_file_is_named_without_being_applied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Discoverable without being self-granting."""

    (tmp_path / POLICY_FILENAME).write_text(POLICY_BODY, encoding="utf-8")

    exit_code = main(
        ["run", "--workspace", str(tmp_path), "do nothing"],
        environ={"PATH": str(Path(sys.executable).resolve().parent)},
    )
    output = capsys.readouterr().out

    # The run stops for a missing credential, but the policy notice came first.
    assert exit_code == 1
    assert "COMMAND_POLICY_NOT_LOADED" in output
    assert POLICY_FILENAME in output


def test_no_notice_when_the_workspace_has_no_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        ["run", "--workspace", str(tmp_path), "do nothing"],
        environ={"PATH": str(Path(sys.executable).resolve().parent)},
    )

    assert "COMMAND_POLICY_NOT_LOADED" not in capsys.readouterr().out


def test_a_named_policy_that_cannot_load_stops_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A policy that partly applied would be an allowance nobody reviewed."""

    broken = tmp_path / POLICY_FILENAME
    broken.write_text("schema_version = 99\n", encoding="utf-8")

    exit_code = main(
        ["run", "--workspace", str(tmp_path), "--command-policy", str(broken), "do nothing"],
        environ=_environment(),
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "POLICY_INVALID" in output
    assert "termination=configuration_error" in output


def test_a_missing_policy_path_is_reported_rather_than_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "run",
            "--workspace",
            str(tmp_path),
            "--command-policy",
            str(tmp_path / "absent.toml"),
            "do nothing",
        ],
        environ=_environment(),
    )

    assert exit_code == 1
    assert "POLICY_NOT_FOUND" in capsys.readouterr().out


@pytest.mark.parametrize("mode", [ApprovalMode.NEVER.value, ApprovalMode.ON_RISK.value])
def test_both_approval_modes_are_accepted(tmp_path: Path, mode: str) -> None:
    exit_code = main(
        ["run", "--workspace", str(tmp_path), "--approval", mode, "do nothing"],
        environ={"PATH": str(Path(sys.executable).resolve().parent)},
    )

    # Stops for the missing credential, which means the flag parsed and the run began.
    assert exit_code == 1


def test_an_unknown_approval_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            ["run", "--workspace", str(tmp_path), "--approval", "always", "do nothing"],
            environ=_environment(),
        )


# --- browser -------------------------------------------------------------


def test_a_pending_request_appears_in_the_summary_and_is_answered(tmp_path: Path) -> None:
    """The run thread waits; the decision arrives on another thread."""

    session = _session()
    outcomes: list[ApprovalOutcome] = []

    def wait() -> None:
        outcomes.append(session.await_approval(_request(), timeout=5.0))

    worker = threading.Thread(target=wait)
    worker.start()
    pending = _spin_until_pending(session)

    assert pending is not None
    assert pending["display_argv"] == ["git", "commit", "-m", "wip"]
    assert session.answer_approval(str(pending["digest"]), ApprovalOutcome.APPROVED) == "accepted"
    worker.join(timeout=5.0)

    assert outcomes == [ApprovalOutcome.APPROVED]
    assert session.pending_approval is None


def test_a_decision_for_a_different_command_is_refused(tmp_path: Path) -> None:
    """An approval must never land on a command nobody looked at."""

    session = _session()
    outcomes: list[ApprovalOutcome] = []

    def wait() -> None:
        outcomes.append(session.await_approval(_request(), timeout=2.0))

    worker = threading.Thread(target=wait)
    worker.start()
    _spin_until_pending(session)

    assert session.answer_approval(_request("different").digest, ApprovalOutcome.APPROVED) == (
        "stale"
    )
    assert session.answer_approval(_request().digest, ApprovalOutcome.DENIED) == "accepted"
    worker.join(timeout=5.0)

    assert outcomes == [ApprovalOutcome.DENIED]


def test_an_unanswered_request_times_out_and_refuses() -> None:
    session = _session()

    outcome = session.await_approval(_request(), timeout=0.05)

    assert outcome is ApprovalOutcome.TIMED_OUT
    assert session.pending_approval is None


def test_a_stop_while_a_request_is_on_screen_refuses_it() -> None:
    """Pressing stop must not leave a command waiting to be approved."""

    session = _session()
    outcomes: list[ApprovalOutcome] = []

    def wait() -> None:
        outcomes.append(session.await_approval(_request(), timeout=5.0))

    worker = threading.Thread(target=wait)
    worker.start()
    _spin_until_pending(session)
    session.request_cancel()
    worker.join(timeout=5.0)

    assert outcomes == [ApprovalOutcome.DENIED]


def test_answering_when_nothing_is_pending_reports_it() -> None:
    session = _session()

    assert session.answer_approval("whatever", ApprovalOutcome.APPROVED) == "none"


def _spin_until_pending(session: BrowserRun) -> dict[str, object] | None:
    for _ in range(500):
        pending = session.pending_approval
        if pending is not None:
            return pending
        threading.Event().wait(0.01)
    return None


# --- the HTTP surface ----------------------------------------------------


def _router(session: BrowserRun | None) -> ApiRouter:
    """Build a real router over a manager holding exactly the session under test."""

    manager = BrowserRunManager(environ=_environment())
    if session is not None:
        manager._sessions[session.run_id] = session
    return ApiRouter(sessions=manager, environ=_environment())


def _post(router: ApiRouter, run_id: str, body: dict[str, object]):
    return router.handle(ApiRequest("POST", f"/api/runs/{run_id}/approval", {}, body))


def test_the_http_route_refuses_an_unknown_run() -> None:
    response = _post(_router(None), "b" * 32, {"request_digest": "x", "decision": "approve"})

    assert response.status == 404
    assert response.body["error"]["code"] == "UNKNOWN_RUN"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"decision": "approve"}, "APPROVAL_DIGEST_REQUIRED"),
        ({"request_digest": "x"}, "INVALID_DECISION"),
        ({"request_digest": "x", "decision": "maybe"}, "INVALID_DECISION"),
    ],
)
def test_the_http_route_validates_its_body(body: dict[str, object], code: str) -> None:
    session = _session()

    response = _post(_router(session), session.run_id, body)

    assert response.status == 400
    assert response.body["error"]["code"] == code


def test_the_http_route_reports_a_stale_digest_with_the_current_request() -> None:
    session = _session()
    worker = threading.Thread(target=lambda: session.await_approval(_request(), timeout=2.0))
    worker.start()
    _spin_until_pending(session)

    response = _post(
        _router(session),
        session.run_id,
        {"request_digest": _request("different").digest, "decision": "approve"},
    )
    session.answer_approval(_request().digest, ApprovalOutcome.DENIED)
    worker.join(timeout=5.0)

    assert response.status == 409
    assert response.body["error"]["code"] == "APPROVAL_CHANGED"
    assert response.body["run"]["run_id"] == session.run_id


def test_the_http_route_reports_when_nothing_is_pending() -> None:
    session = _session()

    response = _post(_router(session), session.run_id, {"request_digest": "x", "decision": "deny"})

    assert response.status == 409
    assert response.body["error"]["code"] == "NO_PENDING_APPROVAL"


def test_a_finished_session_reports_no_pending_approval() -> None:
    session = _session()
    session.status = BrowserRunStatus.FINISHED

    assert session.summary().to_dict()["pending_approval"] is None
