"""Cross-run sessions: what one run is allowed to know about the runs before it.

A session is the only data that crosses a run boundary, so this module is written around
one rule: everything here ends up in a prompt and nowhere else. It never produces a
``RunState`` field, and in particular it can never produce verification evidence -- the
previous run's verification is carried, but carried marked expired, because the safety of
that boundary comes from the new run's state being empty and not from hiding the fact.

The stored file lives under the workspace's run-artifact directory. Write tools refuse
that directory, but an allowed workspace process can still write it, so a session file is
forgeable: the model-authored half of every record is treated as untrusted content, the
file carries nothing that changes a decision, and it loads all-or-nothing.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from proofcoder.errors import ProofCoderError
from proofcoder.protocol import RunResult
from proofcoder.safety.secrets import redact_text
from proofcoder.safety.writes import (
    commit_replacement,
    discard_temporary_file,
    stage_temporary_file,
)

SESSION_SCHEMA_VERSION = 1
SESSIONS_RELATIVE_ROOT = PurePosixPath(".proofcoder/sessions")
SESSION_FILENAME = "session.json"
SESSION_ID_PATTERN = re.compile(r"\A[0-9a-f]{32}\Z")

# Bounds on one stored session. They exist so that a forged file cannot blow the context
# or the wall clock, which is why they are enforced on load and not only on write.
MAX_SESSION_BYTES = 256 * 1024
MAX_SESSION_RUNS = 32
MAX_TASK_CHARACTERS = 4000
MAX_SUMMARY_CHARACTERS = 4000
MAX_LIMITATIONS = 64
MAX_LIMITATION_CHARACTERS = 2000
MAX_BLOCKED_REASON_CHARACTERS = 4000
MAX_CHANGED_FILES = 256
MAX_PATH_CHARACTERS = 1024
MAX_ARGV_ITEMS = 64
MAX_ARGV_CHARACTERS = 4096
MAX_TIMESTAMP_CHARACTERS = 64
MAX_STATUS_CHARACTERS = 64

# Bounds on how many sessions one workspace keeps.
MAX_WORKSPACE_SESSIONS = 16
MAX_WORKSPACE_SESSION_BYTES = 4 * 1024 * 1024

# The share of the context budget the carry may occupy. A long session must not make a
# run impossible before its first model call, so the carry is trimmed to this ceiling
# before the run starts rather than by the in-run compactor, which would drop the oldest
# groups first -- exactly the ones worth keeping.
CARRY_BUDGET_RATIO = 0.125

_EXPIRED_NOTE = "[EXPIRED - this does not verify the current run]"


class SessionError(ProofCoderError):
    """A stable, non-sensitive session failure safe to show the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SessionVerification:
    """One earlier run's accepted verification command, as the program recorded it."""

    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None

    def to_dict(self) -> dict[str, object]:
        """Render this verification for storage."""

        return {"argv": list(self.argv), "cwd": self.cwd, "exit_code": self.exit_code}


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One finished run, split into what the program observed and what the model said.

    The split is not cosmetic. The changed-file list is program-recorded and the summary
    is model-authored; merged into one block, a reader -- and the next run's model --
    would have no way to tell which half can be trusted.
    """

    run_id: str
    task: str
    recorded_at: str
    termination_reason: str
    completion_status: str | None
    changed_files: tuple[str, ...]
    verification: SessionVerification | None
    model_calls: int
    tool_calls: int
    summary: str | None
    limitations: tuple[str, ...]
    blocked_reason: str | None

    @property
    def has_account(self) -> bool:
        """Return whether this record carries anything the model said about itself."""

        return bool(self.summary or self.limitations or self.blocked_reason)

    def without_account(self) -> RunRecord:
        """Return this record with the model-authored half removed."""

        return replace(self, summary=None, limitations=(), blocked_reason=None)

    def to_dict(self) -> dict[str, object]:
        """Render this record for storage."""

        return {
            "blocked_reason": self.blocked_reason,
            "changed_files": list(self.changed_files),
            "completion_status": self.completion_status,
            "limitations": list(self.limitations),
            "model_calls": self.model_calls,
            "recorded_at": self.recorded_at,
            "run_id": self.run_id,
            "summary": self.summary,
            "task": self.task,
            "termination_reason": self.termination_reason,
            "tool_calls": self.tool_calls,
            "verification": None if self.verification is None else self.verification.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Session:
    """An immutable snapshot of one stored session."""

    session_id: str
    created_at: str
    ended_at: str | None
    runs: tuple[RunRecord, ...]

    @property
    def ended(self) -> bool:
        """Return whether this session refuses further runs."""

        return self.ended_at is not None

    def to_dict(self) -> dict[str, object]:
        """Render this session for storage."""

        return {
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "runs": [record.to_dict() for record in self.runs],
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
        }


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Compact facts used to list the sessions of one workspace."""

    session_id: str
    created_at: str
    ended_at: str | None
    run_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class SessionCarry:
    """The text one run carries, together with everything an audit needs about it."""

    session_id: str
    text: str
    carried_runs: int
    dropped_runs: int
    carried_accounts: int
    limit_bytes: int
    prior_verification: SessionVerification | None

    @property
    def byte_count(self) -> int:
        """Return the UTF-8 size of the carried text."""

        return len(self.text.encode("utf-8"))


def validate_session_id(session_id: str) -> str:
    """Reject paths, traversal, and every non-local session-id form."""

    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise SessionError(
            "INVALID_SESSION_ID",
            "session_id must be exactly 32 lowercase hexadecimal characters",
        )
    return session_id


def new_session_id() -> str:
    """Return a locally generated opaque session identifier."""

    return uuid.uuid4().hex


def sessions_root(workspace: Path) -> Path:
    """Return the directory holding every session of one workspace."""

    return workspace / SESSIONS_RELATIVE_ROOT


def session_path(workspace: Path, session_id: str) -> Path:
    """Return the stored file for one session."""

    return sessions_root(workspace) / validate_session_id(session_id) / SESSION_FILENAME


def create_session(
    workspace: Path,
    *,
    session_id: str | None = None,
    now: str | None = None,
) -> Session:
    """Create and store one empty session for this workspace."""

    workspace_root = _workspace_root(workspace)
    identifier = new_session_id() if session_id is None else validate_session_id(session_id)
    session = Session(
        session_id=identifier,
        created_at=_timestamp() if now is None else now,
        ended_at=None,
        runs=(),
    )
    prune_workspace_sessions(workspace_root, keep_session_id=identifier, reserve=1)
    _store(workspace_root, session)
    return session


def load_session(workspace: Path, session_id: str) -> Session:
    """Read and validate one stored session, all-or-nothing.

    A partly accepted session would be a carry nobody reviewed, so a single bad field
    fails the whole file rather than the record that carries it.
    """

    workspace_root = _workspace_root(workspace)
    path = session_path(workspace_root, session_id)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise SessionError("SESSION_NOT_FOUND", "the named session does not exist") from None
    if not resolved.is_file() or path.is_symlink():
        raise SessionError("SESSION_NOT_A_FILE", "the session path is not a regular file")

    try:
        raw = resolved.read_bytes()
    except OSError:
        raise SessionError("SESSION_UNREADABLE", "the session file could not be read") from None
    if len(raw) > MAX_SESSION_BYTES:
        raise SessionError(
            "SESSION_TOO_LARGE",
            f"the session file exceeds {MAX_SESSION_BYTES} bytes",
        )

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SessionError("SESSION_INVALID", "the session file must be valid UTF-8 JSON") from None

    session = _parse_session(document)
    if session.session_id != session_id:
        raise SessionError(
            "SESSION_INVALID",
            "the stored session_id does not match the directory it is stored in",
        )
    return session


def list_sessions(workspace: Path) -> tuple[SessionSummary, ...]:
    """Return every readable session of one workspace, newest first."""

    workspace_root = _workspace_root(workspace)
    summaries: list[SessionSummary] = []
    for session, size in _iter_stored_sessions(workspace_root):
        summaries.append(
            SessionSummary(
                session_id=session.session_id,
                created_at=session.created_at,
                ended_at=session.ended_at,
                run_count=len(session.runs),
                byte_count=size,
            )
        )
    summaries.sort(key=lambda item: (item.created_at, item.session_id), reverse=True)
    return tuple(summaries)


def append_run_record(workspace: Path, session_id: str, record: RunRecord) -> Session:
    """Append one finished run to a session and store the result."""

    workspace_root = _workspace_root(workspace)
    session = load_session(workspace_root, session_id)
    if session.ended:
        raise SessionError("SESSION_ENDED", "this session has ended and accepts no further runs")
    runs = (*session.runs, record)[-MAX_SESSION_RUNS:]
    updated = replace(session, runs=runs)
    prune_workspace_sessions(workspace_root, keep_session_id=session_id)
    _store(workspace_root, updated)
    return updated


def end_session(workspace: Path, session_id: str, *, now: str | None = None) -> Session:
    """Mark one session as ended, keeping its records readable."""

    workspace_root = _workspace_root(workspace)
    session = load_session(workspace_root, session_id)
    if session.ended:
        return session
    updated = replace(session, ended_at=_timestamp() if now is None else now)
    _store(workspace_root, updated)
    return updated


def delete_session(workspace: Path, session_id: str) -> None:
    """Remove one stored session and its directory."""

    workspace_root = _workspace_root(workspace)
    directory = sessions_root(workspace_root) / validate_session_id(session_id)
    if not directory.is_dir():
        raise SessionError("SESSION_NOT_FOUND", "the named session does not exist")
    _remove_session_directory(directory)


def prune_workspace_sessions(
    workspace: Path,
    *,
    keep_session_id: str | None = None,
    reserve: int = 0,
) -> int:
    """Drop the oldest sessions past the retention bounds and return how many went.

    The session a run is about to write is never a candidate, which is the same rule the
    checkpoint retention follows for the run currently in progress. ``reserve`` leaves
    room for a session that is not stored yet, so cleaning before the write cannot leave
    the workspace one over the bound afterwards.
    """

    workspace_root = _workspace_root(workspace)
    limit = max(0, MAX_WORKSPACE_SESSIONS - reserve)
    stored = sorted(
        _iter_stored_sessions(workspace_root),
        key=lambda item: (item[0].created_at, item[0].session_id),
    )
    total_bytes = sum(size for _, size in stored)
    removed = 0
    for session, size in stored:
        over_count = len(stored) - removed > limit
        over_bytes = total_bytes > MAX_WORKSPACE_SESSION_BYTES
        if not over_count and not over_bytes:
            break
        if session.session_id == keep_session_id:
            continue
        _remove_session_directory(sessions_root(workspace_root) / session.session_id)
        total_bytes -= size
        removed += 1
    return removed


def run_record_from_result(
    result: RunResult,
    *,
    task: str,
    now: str | None = None,
    sensitive_values: tuple[str, ...] = (),
) -> RunRecord:
    """Build one session record from a finished run.

    Redaction happens here rather than on read, so a credential never reaches the stored
    file in the first place -- the same rule the trace follows.
    """

    verification = (
        None
        if result.verification_command is None or result.verification_cwd is None
        else SessionVerification(
            argv=tuple(result.verification_command),
            cwd=result.verification_cwd,
            exit_code=result.verification_exit_code,
        )
    )
    return RunRecord(
        run_id=result.run_id,
        task=_clean(task, MAX_TASK_CHARACTERS, sensitive_values),
        recorded_at=_timestamp() if now is None else now,
        termination_reason=result.termination_reason.value,
        completion_status=None
        if result.completion_status is None
        else result.completion_status.value,
        changed_files=tuple(result.changed_files[:MAX_CHANGED_FILES]),
        verification=verification,
        model_calls=result.model_call_count,
        tool_calls=result.tool_call_count,
        summary=None
        if result.finish_summary is None
        else _clean(result.finish_summary, MAX_SUMMARY_CHARACTERS, sensitive_values),
        limitations=tuple(
            _clean(item, MAX_LIMITATION_CHARACTERS, sensitive_values)
            for item in result.limitations[:MAX_LIMITATIONS]
        ),
        blocked_reason=None
        if result.blocked_reason is None
        else _clean(result.blocked_reason, MAX_BLOCKED_REASON_CHARACTERS, sensitive_values),
    )


def build_session_carry(
    session: Session,
    *,
    context_budget_bytes: int,
    ratio: float = CARRY_BUDGET_RATIO,
) -> SessionCarry:
    """Assemble the bounded text one run carries from its session.

    Trimming is deterministic and follows one order: the oldest run's own account goes
    first, then the oldest run's whole record, oldest to newest, until the text fits.
    """

    if context_budget_bytes < 1:
        raise ValueError("context budget must be positive")
    if not 0 < ratio <= 1:
        raise ValueError("carry ratio must be in (0, 1]")
    limit = max(1, int(context_budget_bytes * ratio))

    candidates = list(session.runs)
    if not candidates:
        return SessionCarry(
            session_id=session.session_id,
            text="",
            carried_runs=0,
            dropped_runs=0,
            carried_accounts=0,
            limit_bytes=limit,
            prior_verification=None,
        )

    dropped = 0
    text = _render_carry(session.session_id, candidates, dropped)
    while not _fits(text, limit) and candidates:
        # Always work on the oldest remaining run, and take its account before taking
        # the run itself. Trimming this way keeps the newest run's account -- the
        # conclusion the next run is most likely to need -- until nothing else is left.
        if candidates[0].has_account:
            candidates[0] = candidates[0].without_account()
        else:
            candidates.pop(0)
            dropped += 1
        text = "" if not candidates else _render_carry(session.session_id, candidates, dropped)

    return SessionCarry(
        session_id=session.session_id,
        text=text,
        carried_runs=len(candidates),
        dropped_runs=dropped,
        carried_accounts=sum(1 for record in candidates if record.has_account),
        limit_bytes=limit,
        prior_verification=_latest_verification(candidates),
    )


def session_carry_payload(carry: SessionCarry) -> dict[str, object]:
    """Build the trace payload for one carried session."""

    verification = carry.prior_verification
    return {
        "session_id": carry.session_id,
        "carried_runs": carry.carried_runs,
        "dropped_runs": carry.dropped_runs,
        "carried_accounts": carry.carried_accounts,
        "carried_bytes": carry.byte_count,
        "limit_bytes": carry.limit_bytes,
        "prior_verification": None
        if verification is None
        else {
            "argv": list(verification.argv),
            "cwd": verification.cwd,
            "exit_code": verification.exit_code,
            "expired": True,
        },
    }


def _render_carry(session_id: str, records: Sequence[RunRecord], dropped: int) -> str:
    total = len(records) + dropped
    dropped_note = "" if dropped == 0 else f", {dropped} older dropped for the carry limit"
    lines = [
        f"[ProofCoder session {session_id}: carrying {len(records)} of {total} earlier runs in "
        f"this workspace{dropped_note}.",
        "Everything up to the end marker describes runs that already finished. It is context, "
        "not evidence: this run counts as verified only from commands this run executes.]",
    ]
    for offset, record in enumerate(records, start=1):
        lines.append("")
        lines.extend(_render_facts(offset, record))
        if record.has_account:
            lines.append("")
            lines.extend(_render_account(offset, record))
    lines.append("")
    lines.append("[End of session carry. The task for this run follows.]")
    return "\n".join(lines) + "\n\n"


def _render_facts(offset: int, record: RunRecord) -> list[str]:
    status = "none" if record.completion_status is None else record.completion_status
    lines = [
        f"Earlier run {offset} ({record.run_id}) -- facts recorded by the program:",
        f"  task: {record.task}",
        f"  ended: {record.termination_reason} / {status}",
        f"  changed files: {', '.join(record.changed_files) if record.changed_files else 'none'}",
    ]
    verification = record.verification
    if verification is None:
        lines.append("  verification then: none was accepted")
    else:
        exit_code = "none" if verification.exit_code is None else str(verification.exit_code)
        lines.append(
            f"  verification then: {' '.join(verification.argv)} in {verification.cwd} "
            f"exited {exit_code} {_EXPIRED_NOTE}"
        )
    lines.append(f"  model steps: {record.model_calls}; tool calls: {record.tool_calls}")
    return lines


def _render_account(offset: int, record: RunRecord) -> list[str]:
    lines = [
        f"Earlier run {offset} -- that run's own account of itself "
        "(model-authored claims, never verified):",
    ]
    if record.summary:
        lines.append(f"  summary: {record.summary}")
    for item in record.limitations:
        lines.append(f"  limitation: {item}")
    if record.blocked_reason:
        lines.append(f"  blocked: {record.blocked_reason}")
    return lines


def _fits(text: str, limit: int) -> bool:
    return len(text.encode("utf-8")) <= limit


def _latest_verification(records: Sequence[RunRecord]) -> SessionVerification | None:
    for record in reversed(records):
        if record.verification is not None:
            return record.verification
    return None


def _clean(value: str, limit: int, sensitive_values: tuple[str, ...]) -> str:
    return redact_text(value, sensitive_values=sensitive_values)[:limit]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _workspace_root(workspace: Path) -> Path:
    resolved = workspace.resolve(strict=True)
    if not resolved.is_dir():
        raise SessionError("INVALID_WORKSPACE", "workspace must be an existing directory")
    return resolved


def _store(workspace: Path, session: Session) -> None:
    directory = sessions_root(workspace) / session.session_id
    payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_SESSION_BYTES:
        raise SessionError(
            "SESSION_TOO_LARGE",
            f"the session would exceed {MAX_SESSION_BYTES} bytes",
        )
    target = directory / SESSION_FILENAME
    temporary: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = stage_temporary_file(target, encoded)
        commit_replacement(temporary, target)
        temporary = None
    except OSError:
        raise SessionError("SESSION_WRITE_FAILED", "the session could not be written") from None
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)


def _iter_stored_sessions(workspace: Path) -> list[tuple[Session, int]]:
    root = sessions_root(workspace)
    if not root.is_dir():
        return []
    found: list[tuple[Session, int]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or SESSION_ID_PATTERN.fullmatch(entry.name) is None:
            continue
        path = entry / SESSION_FILENAME
        try:
            size = path.stat().st_size
            session = load_session(workspace, entry.name)
        except (OSError, SessionError):
            # A listing must not fail because one stored session is unreadable; loading
            # that session by name still reports exactly why.
            continue
        found.append((session, size))
    return found


def _remove_session_directory(directory: Path) -> None:
    try:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
        directory.rmdir()
    except OSError:
        raise SessionError("SESSION_WRITE_FAILED", "the session could not be removed") from None


_SESSION_FIELDS = frozenset({"created_at", "ended_at", "runs", "schema_version", "session_id"})
_RECORD_FIELDS = frozenset(
    {
        "blocked_reason",
        "changed_files",
        "completion_status",
        "limitations",
        "model_calls",
        "recorded_at",
        "run_id",
        "summary",
        "task",
        "termination_reason",
        "tool_calls",
        "verification",
    }
)
_VERIFICATION_FIELDS = frozenset({"argv", "cwd", "exit_code"})


def _parse_session(document: object) -> Session:
    table = _require_table(document, "the session file")
    _reject_unknown(table, _SESSION_FIELDS, "session")
    version = table.get("schema_version")
    if type(version) is not int or version != SESSION_SCHEMA_VERSION:
        raise SessionError(
            "SESSION_INVALID",
            f"session schema_version must be {SESSION_SCHEMA_VERSION}",
        )
    identifier = table.get("session_id")
    if not isinstance(identifier, str) or SESSION_ID_PATTERN.fullmatch(identifier) is None:
        raise SessionError("SESSION_INVALID", "session_id must be 32 lowercase hexadecimal digits")

    runs = table.get("runs")
    if not isinstance(runs, list):
        raise SessionError("SESSION_INVALID", "session runs must be a list")
    if len(runs) > MAX_SESSION_RUNS:
        raise SessionError(
            "SESSION_INVALID",
            f"the session stores more than {MAX_SESSION_RUNS} runs",
        )
    return Session(
        session_id=identifier,
        created_at=_text(table.get("created_at"), "created_at", MAX_TIMESTAMP_CHARACTERS),
        ended_at=_optional_text(table.get("ended_at"), "ended_at", MAX_TIMESTAMP_CHARACTERS),
        runs=tuple(_parse_record(item) for item in runs),
    )


def _parse_record(value: object) -> RunRecord:
    table = _require_table(value, "each session run")
    _reject_unknown(table, _RECORD_FIELDS, "session run")
    run_id = table.get("run_id")
    if not isinstance(run_id, str) or SESSION_ID_PATTERN.fullmatch(run_id) is None:
        raise SessionError("SESSION_INVALID", "run_id must be 32 lowercase hexadecimal digits")
    return RunRecord(
        run_id=run_id,
        task=_text(table.get("task"), "task", MAX_TASK_CHARACTERS),
        recorded_at=_text(table.get("recorded_at"), "recorded_at", MAX_TIMESTAMP_CHARACTERS),
        termination_reason=_text(
            table.get("termination_reason"), "termination_reason", MAX_STATUS_CHARACTERS
        ),
        completion_status=_optional_text(
            table.get("completion_status"), "completion_status", MAX_STATUS_CHARACTERS
        ),
        changed_files=_text_list(
            table.get("changed_files"),
            "changed_files",
            MAX_CHANGED_FILES,
            MAX_PATH_CHARACTERS,
        ),
        verification=_parse_verification(table.get("verification")),
        model_calls=_count(table.get("model_calls"), "model_calls"),
        tool_calls=_count(table.get("tool_calls"), "tool_calls"),
        summary=_optional_text(table.get("summary"), "summary", MAX_SUMMARY_CHARACTERS),
        limitations=_text_list(
            table.get("limitations"),
            "limitations",
            MAX_LIMITATIONS,
            MAX_LIMITATION_CHARACTERS,
        ),
        blocked_reason=_optional_text(
            table.get("blocked_reason"), "blocked_reason", MAX_BLOCKED_REASON_CHARACTERS
        ),
    )


def _parse_verification(value: object) -> SessionVerification | None:
    if value is None:
        return None
    table = _require_table(value, "a session verification")
    _reject_unknown(table, _VERIFICATION_FIELDS, "session verification")
    argv = _text_list(table.get("argv"), "argv", MAX_ARGV_ITEMS, MAX_ARGV_CHARACTERS)
    if not argv:
        raise SessionError("SESSION_INVALID", "session verification argv must not be empty")
    exit_code = table.get("exit_code")
    if exit_code is not None and type(exit_code) is not int:
        raise SessionError("SESSION_INVALID", "session verification exit_code must be an integer")
    return SessionVerification(
        argv=argv,
        cwd=_text(table.get("cwd"), "cwd", MAX_PATH_CHARACTERS),
        exit_code=exit_code,
    )


def _require_table(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SessionError("SESSION_INVALID", f"{label} must be a JSON object")
    return value


def _reject_unknown(table: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise SessionError("SESSION_INVALID", f"unknown {label} field: {sorted(unknown)[0]}")


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise SessionError(
            "SESSION_INVALID",
            f"session {field} must be a non-empty string of at most {limit} characters",
        )
    return value


def _optional_text(value: object, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, limit)


def _text_list(value: object, field: str, max_items: int, max_characters: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SessionError("SESSION_INVALID", f"session {field} must be a list of strings")
    if len(value) > max_items:
        raise SessionError(
            "SESSION_INVALID",
            f"session {field} holds more than {max_items} entries",
        )
    return tuple(_text(item, field, max_characters) for item in value)


def _count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise SessionError("SESSION_INVALID", f"session {field} must be a non-negative integer")
    return value
