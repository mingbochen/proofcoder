"""The approval protocol: how a command that needs a person waits for one.

This module owns the semantics and none of the interfaces. Who is asked, and how, is
supplied as a responder; what an unanswered request means is decided here, once, so the
command line and the browser cannot drift into different answers.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 300
MIN_APPROVAL_TIMEOUT_SECONDS = 1
MAX_APPROVAL_TIMEOUT_SECONDS = 3600


class ApprovalMode(StrEnum):
    """How a command that needs confirmation is handled."""

    ON_RISK = "on-risk"
    NEVER = "never"


class ApprovalOutcome(StrEnum):
    """The four ways one approval request can end.

    ``TIMED_OUT`` and ``INTERRUPTED`` are kept distinct from ``DENIED`` even though none
    of the three executes the command: a trace that collapsed them could not answer
    whether anyone ever saw the request.
    """

    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Everything a person needs in order to judge one command."""

    request_id: str
    display_argv: tuple[str, ...]
    relative_cwd: str
    timeout_seconds: int
    command_kind: str
    decision_source: str
    approval_timeout_seconds: int

    def to_dict(self) -> dict[str, object]:
        """Render the request as the payload a caller may show or record."""

        return {
            "approval_timeout_seconds": self.approval_timeout_seconds,
            "command_kind": self.command_kind,
            "cwd": self.relative_cwd,
            "decision_source": self.decision_source,
            "digest": self.digest,
            "display_argv": list(self.display_argv),
            "request_id": self.request_id,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def digest(self) -> str:
        """Bind a decision to this exact command.

        A caller that answers out of band -- the browser, where the page and the run are
        different processes -- must send this back, so an approval can never land on a
        command other than the one whose argv was actually displayed.
        """

        payload = {
            "command_kind": self.command_kind,
            "cwd": self.relative_cwd,
            "decision_source": self.decision_source,
            "display_argv": list(self.display_argv),
            "request_id": self.request_id,
            "timeout_seconds": self.timeout_seconds,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One completed request, ready to be emitted and then forgotten."""

    request: ApprovalRequest
    outcome: ApprovalOutcome
    decided_by: str
    waited_seconds: float

    @property
    def executed(self) -> bool:
        """Return whether this decision permits the command to run."""

        return self.outcome is ApprovalOutcome.APPROVED


ApprovalResponder = Callable[[ApprovalRequest], ApprovalOutcome]


@dataclass(slots=True)
class ApprovalGate:
    """Ask a person about one command, and decide what silence means.

    The gate accumulates how long it waited so the run's time budget can exclude it.
    ``max_seconds`` exists to stop a runaway agent; a person deliberating is not a
    runaway agent, and a run that died because it was supervised would punish exactly
    the behavior approval is meant to encourage.
    """

    mode: ApprovalMode = ApprovalMode.NEVER
    responder: ApprovalResponder | None = None
    timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    clock: Callable[[], float] = time.monotonic
    request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex
    _waited_seconds: float = field(default=0.0, init=False)
    _pending: list[ApprovalRecord] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not (
            MIN_APPROVAL_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_APPROVAL_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "approval timeout_seconds must be between "
                f"{MIN_APPROVAL_TIMEOUT_SECONDS} and {MAX_APPROVAL_TIMEOUT_SECONDS}"
            )

    @property
    def waited_seconds(self) -> float:
        """Total time spent waiting for people, excluded from the run's budget."""

        return self._waited_seconds

    def new_request(
        self,
        *,
        display_argv: tuple[str, ...],
        relative_cwd: str,
        timeout_seconds: int,
        command_kind: str,
        decision_source: str,
    ) -> ApprovalRequest:
        """Build one request, including the identifier its decision must carry back."""

        return ApprovalRequest(
            request_id=self.request_id_factory(),
            display_argv=display_argv,
            relative_cwd=relative_cwd,
            timeout_seconds=timeout_seconds,
            command_kind=command_kind,
            decision_source=decision_source,
            approval_timeout_seconds=self.timeout_seconds,
        )

    def review(self, request: ApprovalRequest) -> ApprovalRecord:
        """Obtain a decision for one request, recording whatever happened.

        Every exit but an explicit approval refuses the command, so an unanswered
        request can never become an executed one.
        """

        if self.mode is ApprovalMode.NEVER:
            # Unattended runs, evaluation included, must never block on a person.
            return self._record(request, ApprovalOutcome.DENIED, "mode", 0.0)
        if self.responder is None:
            # On-risk without anyone to ask is the same fail-closed direction: refuse
            # rather than assume, exactly as the rollback confirmation does off a tty.
            return self._record(request, ApprovalOutcome.DENIED, "unavailable", 0.0)

        started = self.clock()
        try:
            outcome = self.responder(request)
        except KeyboardInterrupt:
            self._record(
                request,
                ApprovalOutcome.INTERRUPTED,
                "interrupt",
                max(0.0, self.clock() - started),
            )
            raise
        waited = max(0.0, self.clock() - started)
        if not isinstance(outcome, ApprovalOutcome) or outcome is ApprovalOutcome.INTERRUPTED:
            # A responder that answered with something this module did not define is a
            # bug, and a bug must not be read as consent.
            return self._record(request, ApprovalOutcome.DENIED, "unavailable", waited)
        return self._record(request, outcome, self._decided_by(outcome), waited)

    def drain(self) -> tuple[ApprovalRecord, ...]:
        """Take the records made since the last drain, for the caller to emit."""

        drained = tuple(self._pending)
        self._pending.clear()
        return drained

    def _record(
        self,
        request: ApprovalRequest,
        outcome: ApprovalOutcome,
        decided_by: str,
        waited: float,
    ) -> ApprovalRecord:
        self._waited_seconds += waited
        record = ApprovalRecord(
            request=request,
            outcome=outcome,
            decided_by=decided_by,
            waited_seconds=waited,
        )
        self._pending.append(record)
        return record

    @staticmethod
    def _decided_by(outcome: ApprovalOutcome) -> str:
        return "timeout" if outcome is ApprovalOutcome.TIMED_OUT else "responder"


def approval_request_payload(request: ApprovalRequest) -> dict[str, object]:
    """Build the trace payload for one approval request."""

    return {"phase": "request", **request.to_dict()}


def approval_decision_payload(record: ApprovalRecord) -> dict[str, object]:
    """Build the trace payload for one approval decision."""

    return {
        "phase": "decision",
        "request_id": record.request.request_id,
        "outcome": record.outcome.value,
        "decided_by": record.decided_by,
        "executed": record.executed,
        "waited_seconds": round(record.waited_seconds, 3),
    }
