"""Offline tests for the approval protocol and its four exits."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import proofcoder.safety.commands as command_policy
from proofcoder.agent import AgentLoop
from proofcoder.approval import (
    ApprovalGate,
    ApprovalMode,
    ApprovalOutcome,
    ApprovalRequest,
    approval_decision_payload,
    approval_request_payload,
)
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, TerminationReason, ToolCall
from proofcoder.tools.base import ToolResult
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _stable_executable_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        command_policy.shutil,
        "which",
        lambda executable, *, path: str(Path(sys.executable).resolve()),
    )


def _environment() -> dict[str, str]:
    return {"PATH": str(Path(sys.executable).resolve().parent)}


def _request(gate: ApprovalGate) -> ApprovalRequest:
    return gate.new_request(
        display_argv=("git", "commit", "-m", "wip"),
        relative_cwd=".",
        timeout_seconds=60,
        command_kind="git_write",
        decision_source="builtin",
    )


def _gate(
    responder: Callable[[ApprovalRequest], ApprovalOutcome] | None,
    *,
    mode: ApprovalMode = ApprovalMode.ON_RISK,
    times: list[float] | None = None,
) -> ApprovalGate:
    ticks = iter(times or [0.0, 0.0])
    return ApprovalGate(
        mode=mode,
        responder=responder,
        clock=lambda: next(ticks),
        request_id_factory=lambda: "request-1",
    )


def test_an_approved_request_permits_the_command(tmp_path: Path) -> None:
    gate = _gate(lambda _request: ApprovalOutcome.APPROVED)

    record = gate.review(_request(gate))

    assert record.outcome is ApprovalOutcome.APPROVED
    assert record.executed is True
    assert record.decided_by == "responder"


@pytest.mark.parametrize(
    ("outcome", "decided_by"),
    [(ApprovalOutcome.DENIED, "responder"), (ApprovalOutcome.TIMED_OUT, "timeout")],
)
def test_every_answer_but_approval_refuses_the_command(
    outcome: ApprovalOutcome, decided_by: str
) -> None:
    """Denial and timeout are separate records, and neither executes anything."""

    gate = _gate(lambda _request: outcome)

    record = gate.review(_request(gate))

    assert record.outcome is outcome
    assert record.executed is False
    assert record.decided_by == decided_by


def test_an_interrupted_wait_is_recorded_before_the_interrupt_propagates() -> None:
    """An unanswered request must never reach the trace as an approved one."""

    def interrupt(_request: ApprovalRequest) -> ApprovalOutcome:
        raise KeyboardInterrupt

    gate = _gate(interrupt, times=[0.0, 2.0])

    with pytest.raises(KeyboardInterrupt):
        gate.review(_request(gate))

    (record,) = gate.drain()
    assert record.outcome is ApprovalOutcome.INTERRUPTED
    assert record.executed is False
    assert record.decided_by == "interrupt"
    assert record.waited_seconds == pytest.approx(2.0)


def test_never_mode_refuses_without_asking_anyone() -> None:
    """Evaluation runs in this mode, so a fixture can never block on a person."""

    asked: list[ApprovalRequest] = []

    def responder(request: ApprovalRequest) -> ApprovalOutcome:
        asked.append(request)
        return ApprovalOutcome.APPROVED

    gate = _gate(responder, mode=ApprovalMode.NEVER)

    record = gate.review(_request(gate))

    assert asked == []
    assert record.outcome is ApprovalOutcome.DENIED
    assert record.decided_by == "mode"
    assert gate.waited_seconds == 0.0


def test_on_risk_without_anyone_to_ask_refuses_rather_than_assumes() -> None:
    gate = _gate(None)

    record = gate.review(_request(gate))

    assert record.outcome is ApprovalOutcome.DENIED
    assert record.decided_by == "unavailable"


def test_a_responder_that_answers_with_nonsense_is_not_read_as_consent() -> None:
    gate = _gate(lambda _request: "yes")  # type: ignore[arg-type,return-value]

    record = gate.review(_request(gate))

    assert record.outcome is ApprovalOutcome.DENIED
    assert record.decided_by == "unavailable"


def test_waiting_time_accumulates_and_records_drain_once() -> None:
    gate = _gate(lambda _request: ApprovalOutcome.APPROVED, times=[0.0, 1.5, 10.0, 12.25])

    gate.review(_request(gate))
    gate.review(_request(gate))

    assert gate.waited_seconds == pytest.approx(3.75)
    drained = gate.drain()
    assert [record.waited_seconds for record in drained] == pytest.approx([1.5, 2.25])
    assert gate.drain() == ()


def test_the_digest_binds_a_decision_to_the_command_that_was_displayed() -> None:
    gate = _gate(lambda _request: ApprovalOutcome.APPROVED)
    shown = _request(gate)
    other = gate.new_request(
        display_argv=("git", "commit", "-m", "different"),
        relative_cwd=".",
        timeout_seconds=60,
        command_kind="git_write",
        decision_source="builtin",
    )

    assert shown.digest != other.digest
    assert shown.digest == _request(gate).digest
    assert len(shown.digest) == 64


def test_event_payloads_separate_the_request_from_its_decision() -> None:
    gate = _gate(lambda _request: ApprovalOutcome.TIMED_OUT, times=[0.0, 4.0])
    record = gate.review(_request(gate))

    request_payload = approval_request_payload(record.request)
    decision_payload = approval_decision_payload(record)

    assert request_payload["phase"] == "request"
    assert request_payload["display_argv"] == ["git", "commit", "-m", "wip"]
    assert request_payload["cwd"] == "."
    assert request_payload["timeout_seconds"] == 60
    assert request_payload["decision_source"] == "builtin"
    assert decision_payload == {
        "phase": "decision",
        "request_id": "request-1",
        "outcome": "timed_out",
        "decided_by": "timeout",
        "executed": False,
        "waited_seconds": 4.0,
    }


@pytest.mark.parametrize("timeout", [0, 3601])
def test_an_out_of_range_approval_timeout_is_refused(timeout: int) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        ApprovalGate(timeout_seconds=timeout)


def _run_command(workspace: Path, gate: ApprovalGate, argv: list[str]) -> ToolResult:
    registry = ToolRegistry()
    registry.register(create_run_command_tool(workspace, environ=_environment(), approval=gate))
    return registry.dispatch(
        ToolCall(
            id="command-1",
            function=FunctionCall(
                name="run_command",
                arguments=f'{{"argv": {argv!r}}}'.replace("'", '"'),
            ),
        )
    )


def test_a_refused_command_reports_a_structured_failure_and_does_not_run(
    tmp_path: Path,
) -> None:
    """Refusal is recoverable: the model can read it and take another route."""

    gate = _gate(lambda _request: ApprovalOutcome.DENIED)

    result = _run_command(tmp_path, gate, ["git", "status"])
    refused = _run_command(tmp_path, gate, ["git", "commit", "-m", "wip"])

    # A read-only subcommand never reaches the gate at all.
    assert result.error is None or result.error.code != "APPROVAL_DENIED"
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.code == "APPROVAL_DENIED"
    assert refused.error.retryable is False


def test_a_timed_out_command_is_reported_apart_from_a_denial(tmp_path: Path) -> None:
    gate = _gate(lambda _request: ApprovalOutcome.TIMED_OUT)

    refused = _run_command(tmp_path, gate, ["git", "commit", "-m", "wip"])

    assert refused.error is not None
    assert refused.error.code == "APPROVAL_TIMED_OUT"
    (record,) = gate.drain()
    assert record.outcome is ApprovalOutcome.TIMED_OUT


def test_the_gate_sees_the_command_the_model_actually_asked_for(tmp_path: Path) -> None:
    seen: list[ApprovalRequest] = []

    def responder(request: ApprovalRequest) -> ApprovalOutcome:
        seen.append(request)
        return ApprovalOutcome.DENIED

    gate = _gate(responder)

    _run_command(tmp_path, gate, ["git", "commit", "-m", "wip"])

    assert [request.display_argv for request in seen] == [("git", "commit", "-m", "wip")]
    assert seen[0].command_kind == "git_write"
    assert seen[0].relative_cwd == "."


def _tool_call(argv: list[str], call_id: str = "command-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(
            name="run_command",
            arguments=f'{{"argv": {argv!r}}}'.replace("'", '"'),
        ),
    )


def _loop_with_gate(
    workspace: Path,
    gate: ApprovalGate,
    clock: Callable[[], float],
    loops: list[ScriptedClient],
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(create_run_command_tool(workspace, environ=_environment(), approval=gate))
    client = ScriptedClient(
        [
            ModelResponse(
                content=None,
                reasoning_content=None,
                finish_reason="tool_calls",
                usage=None,
                tool_calls=(_tool_call(["git", "commit", "-m", "wip"]),),
            ),
            *(
                ModelResponse(
                    content="I could not commit.",
                    reasoning_content=None,
                    finish_reason="stop",
                    usage=None,
                )
                for _ in range(4)
            ),
        ]
    )
    loops.append(client)
    return AgentLoop(
        client=client,
        registry=registry,
        workspace=workspace,
        system_prompt="approval system prompt",
        max_steps=4,
        max_seconds=30.0,
        clock=clock,
        sleep=lambda _seconds: None,
        random_value=lambda: 0.0,
        approval=gate,
    )


def test_a_refused_command_does_not_end_the_run(tmp_path: Path) -> None:
    """Refusal is a structured tool result the model can route around, not a stop."""

    gate = _gate(lambda _request: ApprovalOutcome.DENIED, times=[0.0, 0.0])
    clients: list[ScriptedClient] = []
    loop = _loop_with_gate(tmp_path, gate, lambda: 0.0, clients)

    result = loop.run("Commit the change.")

    assert result.tool_error_count == 1
    # The model was asked again after the refusal, which is the whole point.
    assert len(clients[0].requests) > 1
    assert result.termination_reason is not TerminationReason.INTERRUPTED
    assert result.termination_reason is not TerminationReason.MAX_TIME


def test_the_wait_for_a_person_is_excluded_from_the_run_budget(tmp_path: Path) -> None:
    """max_seconds bounds the agent's work; a person deliberating is not that work."""

    # The responder consumes two ticks 20 seconds apart, which is most of the budget.
    approval_ticks = iter([0.0, 20.0])
    gate = ApprovalGate(
        mode=ApprovalMode.ON_RISK,
        responder=lambda _request: ApprovalOutcome.DENIED,
        clock=lambda: next(approval_ticks),
        request_id_factory=lambda: "request-1",
    )
    run_ticks = iter([0.0, 1.0, 2.0, 22.0, 23.0, 24.0, 25.0, 26.0])
    loop = _loop_with_gate(tmp_path, gate, lambda: next(run_ticks, 26.0), [])

    result = loop.run("Commit the change.")

    assert gate.waited_seconds == pytest.approx(20.0)
    # The wall clock advanced past 20 seconds, but the run is not charged for it and so
    # never reaches its 30-second limit.
    assert result.termination_reason is not TerminationReason.MAX_TIME
    assert result.elapsed_seconds < 20.0
