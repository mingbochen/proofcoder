"""Transport-independent JSON handlers for the local ProofCoder web interface.

Every handler is a plain function over parsed input that returns one
:class:`ApiResponse`. Keeping the routing table free of socket, header, and
streaming concerns lets the whole API be tested offline without binding a port,
and keeps the HTTP module limited to framing and access control.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hmac import compare_digest
from pathlib import Path
from typing import Protocol, cast

from proofcoder.agent_runtime import AgentRunLimits
from proofcoder.approval import ApprovalOutcome
from proofcoder.checkpoint import ChangeSource, CheckpointError, RollbackPlan
from proofcoder.config import ProofCoderConfig
from proofcoder.context import DEFAULT_CONTEXT_BUDGET_BYTES
from proofcoder.errors import ConfigurationError
from proofcoder.events import EventType
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.protocol import ModelResponse
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS
from proofcoder.rollback import (
    build_rollback_plan,
    perform_rollback,
    plan_digest,
)
from proofcoder.safety.secrets import sensitive_environment_values
from proofcoder.tools.files import DEFAULT_IGNORED_DIRECTORIES
from proofcoder.trace import (
    TracePathError,
    final_trace_report,
    list_traces,
    read_trace,
    validate_run_id,
)
from proofcoder.web.sessions import SessionError, SessionManager, SessionStatus

MAX_AGENT_STEPS = 64
MAX_AGENT_SECONDS = 3600.0
MIN_CONTEXT_BUDGET_BYTES = 4096
MAX_CONTEXT_BUDGET_BYTES = 2 * 1024 * 1024
MAX_CONSECUTIVE_FAILURES = 32
MAX_BROWSE_ENTRIES = 400
MAX_HISTORY_ENTRIES = 100
MAX_WAIT_SECONDS = 25.0
DEFAULT_HISTORY_ENTRIES = 30
MINIMUM_PYTHON = (3, 11)

DEFAULT_LIMITS = AgentRunLimits()


class _ConnectivityClient(Protocol):
    def check_connection(self) -> ModelResponse: ...


ConnectivityClientFactory = Callable[[ProofCoderConfig], _ConnectivityClient]
_DEFAULT_CONNECTIVITY_FACTORY = cast(ConnectivityClientFactory, DeepSeekClient)


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One JSON body and its HTTP status code."""

    status: int
    body: dict[str, object]


@dataclass(frozen=True, slots=True)
class ApiRequest:
    """One already parsed API request."""

    method: str
    path: str
    query: Mapping[str, str]
    body: Mapping[str, object]


def error_response(status: int, code: str, message: str) -> ApiResponse:
    """Build one stable machine-readable error body."""

    return ApiResponse(status, {"error": {"code": code, "message": message}})


class ApiRouter:
    """Dispatch parsed requests onto session, workspace, and trace handlers."""

    def __init__(
        self,
        *,
        sessions: SessionManager,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        default_workspace: Path | None = None,
        connectivity_factory: ConnectivityClientFactory = _DEFAULT_CONNECTIVITY_FACTORY,
        allow_browse: bool = True,
    ) -> None:
        self._sessions = sessions
        self._environ = environ
        self._cwd = Path.cwd() if cwd is None else cwd
        self._default_workspace = default_workspace
        self._connectivity_factory = connectivity_factory
        self._allow_browse = allow_browse

    def handle(self, request: ApiRequest) -> ApiResponse:
        """Route one request, converting known failures into JSON errors."""

        segments = tuple(part for part in request.path.strip("/").split("/") if part)
        if not segments or segments[0] != "api":
            return error_response(404, "NOT_FOUND", "unknown API path")
        route = segments[1:]
        try:
            return self._dispatch(request, route)
        except SessionError as error:
            return error_response(_session_error_status(error.code), error.code, str(error))
        except TracePathError as error:
            status = 404 if error.code in {"TRACE_NOT_FOUND", "INVALID_RUN_ID"} else 400
            return error_response(status, error.code, str(error))
        except CheckpointError as error:
            status = 404 if error.code in {"CHECKPOINT_NOT_FOUND", "INVALID_RUN_ID"} else 400
            return error_response(status, error.code, str(error))

    def _dispatch(self, request: ApiRequest, route: tuple[str, ...]) -> ApiResponse:
        method = request.method.upper()
        if route == ("status",) and method == "GET":
            return self._status()
        if route == ("doctor",) and method == "POST":
            return self._doctor(request)
        if route == ("workspace",) and method == "POST":
            return self._resolve_workspace(request)
        if route == ("workspace", "browse") and method == "GET":
            return self._browse(request)
        if route == ("runs",) and method == "GET":
            return self._list_runs()
        if route == ("runs",) and method == "POST":
            return self._start_run(request)
        if len(route) == 2 and route[0] == "runs" and method == "GET":
            return self._run_detail(route[1])
        if len(route) == 3 and route[0] == "runs" and route[2] == "events" and method == "GET":
            return self._run_events(request, route[1])
        if len(route) == 3 and route[0] == "runs" and route[2] == "cancel" and method == "POST":
            return self._cancel_run(route[1])
        if len(route) == 3 and route[0] == "runs" and route[2] == "approval" and method == "POST":
            return self._answer_approval(request, route[1])
        if _matches(route, "checkpoints", "plan") and method == "GET":
            return self._rollback_plan(request, route[1])
        if _matches(route, "checkpoints", "rollback") and method == "POST":
            return self._apply_rollback(request, route[1])
        if route == ("traces",) and method == "GET":
            return self._list_traces(request)
        if len(route) == 2 and route[0] == "traces" and method == "GET":
            return self._trace_detail(request, route[1])
        return error_response(404, "NOT_FOUND", "unknown API path")

    def _status(self) -> ApiResponse:
        """Report configuration, local checks, and defaults without any secret."""

        try:
            config = ProofCoderConfig.from_env(offline=True, environ=self._environ)
            configuration_error: str | None = None
        except ConfigurationError as error:
            config = None
            configuration_error = str(error)
        source = os.environ if self._environ is None else self._environ
        raw_key = source.get("DEEPSEEK_API_KEY")
        python_version = ".".join(str(part) for part in sys.version_info[:3])
        try:
            importlib.import_module("proofcoder")
            package_ok = True
        except Exception:  # pragma: no cover - import of a running package cannot fail
            package_ok = False
        default_workspace = self._default_workspace or self._cwd
        return ApiResponse(
            200,
            {
                "model": None if config is None else config.model,
                "base_url": None if config is None else config.base_url,
                "reasoning_effort": None if config is None else config.reasoning_effort,
                "api_key_configured": bool(raw_key and raw_key.strip()),
                "configuration_error": configuration_error,
                "python_version": python_version,
                "python_supported": sys.version_info >= MINIMUM_PYTHON,
                "package_importable": package_ok,
                "default_workspace": str(default_workspace),
                "browse_enabled": self._allow_browse,
                "active_runs": self._sessions.active_count(),
                "limits": {
                    "max_steps": {
                        "default": DEFAULT_LIMITS.max_steps,
                        "minimum": 1,
                        "maximum": MAX_AGENT_STEPS,
                    },
                    "max_seconds": {
                        "default": DEFAULT_LIMITS.max_seconds,
                        "minimum": 1,
                        "maximum": MAX_AGENT_SECONDS,
                    },
                    "context_budget_bytes": {
                        "default": DEFAULT_CONTEXT_BUDGET_BYTES,
                        "minimum": MIN_CONTEXT_BUDGET_BYTES,
                        "maximum": MAX_CONTEXT_BUDGET_BYTES,
                    },
                    "max_consecutive_failures": {
                        "default": DEFAULT_LIMITS.max_consecutive_failures,
                        "minimum": 1,
                        "maximum": MAX_CONSECUTIVE_FAILURES,
                    },
                    "max_api_attempts": {
                        "default": DEFAULT_LIMITS.max_api_attempts,
                        "minimum": 1,
                        "maximum": DEFAULT_MAX_API_ATTEMPTS,
                    },
                },
            },
        )

    def _doctor(self, request: ApiRequest) -> ApiResponse:
        """Run the local checks and, unless offline, one provider connectivity probe."""

        offline = bool(request.body.get("offline", False))
        checks: list[dict[str, object]] = []
        python_version = ".".join(str(part) for part in sys.version_info[:3])
        checks.append(
            {
                "name": "Python",
                "ok": sys.version_info >= MINIMUM_PYTHON,
                "detail": f"{python_version} (requires 3.11+)",
            }
        )
        try:
            importlib.import_module("proofcoder")
            package_ok = True
        except Exception:  # pragma: no cover - import of a running package cannot fail
            package_ok = False
        checks.append(
            {
                "name": "ProofCoder import",
                "ok": package_ok,
                "detail": "available" if package_ok else "failed",
            }
        )
        workspace_ok = self._cwd.is_dir() and os.access(self._cwd, os.R_OK | os.W_OK)
        checks.append(
            {
                "name": "Working directory",
                "ok": workspace_ok,
                "detail": (
                    f"{self._cwd} "
                    f"({'readable and writable' if workspace_ok else 'not readable and writable'})"
                ),
            }
        )

        if offline:
            checks.append(
                {
                    "name": "API connectivity",
                    "ok": True,
                    "detail": "skipped in offline mode",
                }
            )
            return ApiResponse(200, {"offline": True, "checks": checks, "ok": _all_ok(checks)})

        try:
            config = ProofCoderConfig.from_env(environ=self._environ)
        except ConfigurationError as error:
            checks.append({"name": "API connectivity", "ok": False, "detail": str(error)})
            return ApiResponse(200, {"offline": False, "checks": checks, "ok": False})
        try:
            self._connectivity_factory(config).check_connection()
        except Exception:
            checks.append(
                {
                    "name": "API connectivity",
                    "ok": False,
                    "detail": "DeepSeek request failed; check configuration and network.",
                }
            )
            return ApiResponse(200, {"offline": False, "checks": checks, "ok": False})
        checks.append(
            {
                "name": "API connectivity",
                "ok": True,
                "detail": "DeepSeek connection succeeded",
            }
        )
        return ApiResponse(200, {"offline": False, "checks": checks, "ok": _all_ok(checks)})

    def _resolve_workspace(self, request: ApiRequest) -> ApiResponse:
        """Resolve one user-supplied workspace string without creating anything."""

        raw = request.body.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return error_response(400, "INVALID_WORKSPACE", "a workspace path is required")
        resolved = self._absolute(raw.strip())
        exists = resolved.exists()
        is_dir = resolved.is_dir()
        writable = bool(is_dir and os.access(resolved, os.R_OK | os.W_OK))
        body: dict[str, object] = {
            "path": str(resolved),
            "name": resolved.name or str(resolved),
            "exists": exists,
            "is_directory": is_dir,
            "writable": writable,
            "run_count": 0,
        }
        if is_dir:
            try:
                body["run_count"] = len(list_traces(resolved))
            except TracePathError:
                body["run_count"] = 0
        return ApiResponse(200, body)

    def _browse(self, request: ApiRequest) -> ApiResponse:
        """List candidate workspace directories so paths never have to be typed."""

        if not self._allow_browse:
            return error_response(403, "BROWSE_DISABLED", "directory browsing is disabled")
        raw = request.query.get("path", "").strip()
        target = self._absolute(raw) if raw else (self._default_workspace or self._cwd)
        if not target.is_dir():
            return error_response(404, "NOT_A_DIRECTORY", "path is not an existing directory")
        try:
            entries = sorted(
                (item for item in target.iterdir() if _offerable_directory(item)),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            return error_response(403, "BROWSE_DENIED", "directory could not be listed")
        truncated = len(entries) > MAX_BROWSE_ENTRIES
        listed = [{"name": item.name, "path": str(item)} for item in entries[:MAX_BROWSE_ENTRIES]]
        parent = target.parent
        return ApiResponse(
            200,
            {
                "path": str(target),
                "parent": None if parent == target else str(parent),
                "roots": [str(item) for item in _filesystem_roots(self._cwd)],
                "directories": listed,
                "truncated": truncated,
            },
        )

    def _list_runs(self) -> ApiResponse:
        """Return newest-first snapshots of the runs this server started."""

        return ApiResponse(
            200,
            {"runs": [summary.to_dict() for summary in self._sessions.summaries()]},
        )

    def _start_run(self, request: ApiRequest) -> ApiResponse:
        """Validate one browser request and start a bounded agent run."""

        task = request.body.get("task")
        if not isinstance(task, str):
            return error_response(400, "EMPTY_TASK", "a task description is required")
        workspace_value = request.body.get("workspace")
        if not isinstance(workspace_value, str) or not workspace_value.strip():
            return error_response(400, "INVALID_WORKSPACE", "a workspace path is required")
        try:
            limits = _parse_limits(request.body)
        except ValueError as error:
            return error_response(400, "INVALID_LIMIT", str(error))
        session = self._sessions.start(
            workspace=self._absolute(workspace_value.strip()),
            task=task,
            limits=limits,
        )
        return ApiResponse(201, {"run": session.summary().to_dict()})

    def _run_detail(self, run_id: str) -> ApiResponse:
        session = self._sessions.get(run_id)
        if session is None:
            return error_response(404, "RUN_NOT_FOUND", "no retained run has this identifier")
        return ApiResponse(200, {"run": session.summary().to_dict()})

    def _run_events(self, request: ApiRequest, run_id: str) -> ApiResponse:
        """Return buffered events after one cursor, optionally waiting for more.

        The browser long-polls this endpoint instead of opening an EventSource so the
        local session token can travel in a request header rather than in a URL that
        would end up in history and process listings.
        """

        session = self._sessions.get(run_id)
        if session is None:
            return error_response(404, "RUN_NOT_FOUND", "no retained run has this identifier")
        try:
            cursor = _parse_cursor(request.query.get("cursor"))
            wait_seconds = _parse_wait(request.query.get("wait"))
        except ValueError as error:
            return error_response(400, "INVALID_QUERY", str(error))
        if wait_seconds > 0:
            next_cursor, events, _ = session.wait_for_events(cursor, wait_seconds)
        else:
            next_cursor, events = session.events_after(cursor)
        summary = session.summary()
        return ApiResponse(
            200,
            {
                "cursor": next_cursor,
                "events": events,
                "run": summary.to_dict(),
                "done": summary.status is SessionStatus.FINISHED
                and next_cursor >= summary.event_count,
            },
        )

    def _cancel_run(self, run_id: str) -> ApiResponse:
        changed = self._sessions.cancel(run_id)
        session = self._sessions.get(run_id)
        assert session is not None
        return ApiResponse(200, {"cancelled": changed, "run": session.summary().to_dict()})

    def _answer_approval(self, request: ApiRequest, run_id: str) -> ApiResponse:
        """Record one decision for the command a browser is currently showing.

        The page and the run live in different threads, so the decision is bound to the
        request it was given for: the digest the page displays must still name the
        command now awaiting an answer. A stale digest is refused and the caller gets
        whatever is pending instead, so an approval can never land on a command nobody
        looked at.
        """

        session = self._sessions.get(run_id)
        if session is None:
            return error_response(404, "UNKNOWN_RUN", "no run with this identifier")
        submitted = request.body.get("request_digest")
        if not isinstance(submitted, str) or not submitted:
            return error_response(
                400,
                "APPROVAL_DIGEST_REQUIRED",
                "the digest of the request that was shown is required",
            )
        decision = request.body.get("decision")
        if decision not in {"approve", "deny"}:
            return error_response(400, "INVALID_DECISION", "decision must be 'approve' or 'deny'")

        outcome = ApprovalOutcome.APPROVED if decision == "approve" else ApprovalOutcome.DENIED
        status = session.answer_approval(submitted, outcome)
        if status == "none":
            return error_response(
                409, "NO_PENDING_APPROVAL", "this run is not waiting for a decision"
            )
        if status == "stale":
            return ApiResponse(
                409,
                {
                    "error": {
                        "code": "APPROVAL_CHANGED",
                        "message": (
                            "the request that was shown is no longer the one awaiting a "
                            "decision; review the current request before answering"
                        ),
                    },
                    "run": session.summary().to_dict(),
                },
            )
        return ApiResponse(200, {"decision": decision, "run": session.summary().to_dict()})

    def _list_traces(self, request: ApiRequest) -> ApiResponse:
        """Return newest-first stored runs for one workspace."""

        workspace = self._workspace_from_query(request)
        if isinstance(workspace, ApiResponse):
            return workspace
        try:
            limit = _parse_limit(request.query.get("limit"))
        except ValueError as error:
            return error_response(400, "INVALID_LIMIT", str(error))
        summaries = sorted(
            list_traces(workspace),
            key=lambda item: (item.started_at, item.run_id),
            reverse=True,
        )
        entries: list[dict[str, object]] = []
        for summary in summaries[:limit]:
            entry: dict[str, object] = {
                "run_id": summary.run_id,
                "started_at": summary.started_at,
                "status": summary.status,
                "event_count": summary.event_count,
                "trace_complete": summary.trace_complete,
                "task": "",
                "termination_reason": None,
                "completion_status": None,
            }
            try:
                trace = read_trace(workspace, summary.run_id)
            except TracePathError:
                entries.append(entry)
                continue
            entry.update(_trace_headline(trace.events))
            entries.append(entry)
        return ApiResponse(
            200,
            {
                "workspace": str(workspace),
                "total": len(summaries),
                "traces": entries,
            },
        )

    def _trace_detail(self, request: ApiRequest, run_id: str) -> ApiResponse:
        """Replay one stored trace exactly as ``proofcoder trace show`` reads it."""

        workspace = self._workspace_from_query(request)
        if isinstance(workspace, ApiResponse):
            return workspace
        trace = read_trace(workspace, validate_run_id(run_id))
        return ApiResponse(
            200,
            {
                "run_id": trace.run_id,
                "workspace": str(workspace),
                "trace_path": trace.trace_path,
                "trace_complete": trace.trace_complete,
                "report": final_trace_report(trace),
                "events": [event.to_dict() for event in trace.events],
                "issues": [
                    {
                        "code": issue.code,
                        "line_number": issue.line_number,
                        "message": issue.message,
                    }
                    for issue in trace.issues
                ],
                **_trace_headline(trace.events),
            },
        )

    def _rollback_plan(self, request: ApiRequest, run_id: str) -> ApiResponse:
        """Return everything a rollback of one run would change, changing nothing."""

        workspace = self._workspace_from_query(request)
        if isinstance(workspace, ApiResponse):
            return workspace
        plan = build_rollback_plan(workspace, validate_run_id(run_id))
        return ApiResponse(200, _plan_body(workspace, plan))

    def _apply_rollback(self, request: ApiRequest, run_id: str) -> ApiResponse:
        """Apply one plan the caller has seen, refusing anything else.

        The browser cannot be asked at a terminal, so an approval is bound to the plan
        it was given for: the digest of the displayed plan must still match the plan
        that would run now. When the workspace moved in between, the caller gets the
        new plan and has to confirm that one instead.
        """

        raw_workspace = request.body.get("workspace")
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            return error_response(400, "INVALID_WORKSPACE", "a workspace path is required")
        workspace = self._absolute(raw_workspace.strip())
        if not workspace.is_dir():
            return error_response(
                404, "INVALID_WORKSPACE", "workspace must be an existing directory"
            )
        submitted = request.body.get("plan_digest")
        if not isinstance(submitted, str) or not submitted:
            return error_response(
                400,
                "PLAN_DIGEST_REQUIRED",
                "the digest of the plan that was shown is required",
            )
        if self._sessions.workspace_busy(workspace):
            return error_response(
                409,
                "WORKSPACE_BUSY",
                "a run is using this workspace; wait for it or stop it before rolling back",
            )

        validated = validate_run_id(run_id)
        plan = build_rollback_plan(workspace, validated)
        current = plan_digest(plan)
        if not compare_digest(submitted, current):
            body = _plan_body(workspace, plan)
            body["error"] = {
                "code": "PLAN_CHANGED",
                "message": "the workspace changed since this plan was shown; review it again",
            }
            return ApiResponse(409, body)

        recorded = perform_rollback(workspace, plan, sensitive_values=self._sensitive_values())
        result = recorded.result
        return ApiResponse(
            200,
            {
                "workspace": str(workspace),
                "target_run_id": recorded.target_run_id,
                "run_id": recorded.run_id,
                "complete": recorded.result.complete,
                "trace_path": recorded.trace_path,
                "trace_complete": recorded.trace_complete,
                "restored": list(result.restored),
                "recreated": list(result.recreated),
                "deleted": list(result.deleted),
                "directories_created": list(result.directories_created),
                "directories_removed": list(result.directories_removed),
                "skipped": [{"path": skip.path, "reason": skip.reason} for skip in result.skipped],
                "failures": [
                    {"path": failure.path, "action": failure.action.value, "code": failure.code}
                    for failure in result.failures
                ],
            },
        )

    def _sensitive_values(self) -> tuple[str, ...]:
        return sensitive_environment_values(self._environ)

    def _workspace_from_query(self, request: ApiRequest) -> Path | ApiResponse:
        raw = request.query.get("workspace", "").strip()
        if not raw:
            return error_response(400, "INVALID_WORKSPACE", "a workspace path is required")
        workspace = self._absolute(raw)
        if not workspace.is_dir():
            return error_response(
                404, "INVALID_WORKSPACE", "workspace must be an existing directory"
            )
        return workspace

    def _absolute(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self._cwd / candidate
        return candidate.resolve(strict=False)


def _matches(route: tuple[str, ...], prefix: str, suffix: str) -> bool:
    """Return whether one route is the three-segment ``prefix/<id>/suffix`` shape."""

    return len(route) == 3 and route[0] == prefix and route[2] == suffix


def _plan_body(workspace: Path, plan: RollbackPlan) -> dict[str, object]:
    """Serialize one plan together with the digest that confirms it."""

    return {
        "workspace": str(workspace),
        "target_run_id": plan.run_id,
        "plan_digest": plan_digest(plan),
        "empty": plan.empty,
        "items": [
            {
                "action": item.action.value,
                "path": item.path,
                "source": item.source.value,
                "by_tool": item.source is ChangeSource.TOOL,
            }
            for item in plan.items
        ],
        "skipped": [{"path": skip.path, "reason": skip.reason} for skip in plan.skipped],
    }


def _trace_headline(events: Sequence[object]) -> dict[str, object]:
    """Extract the task text and terminal facts from one decoded trace."""

    task = ""
    termination_reason: str | None = None
    completion_status: str | None = None
    changed_files: list[str] = []
    target_run_id: str | None = None
    for event in events:
        event_type = getattr(event, "event_type", None)
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        if event_type is EventType.TASK and not task:
            value = payload.get("task")
            task = value if isinstance(value, str) else ""
        elif event_type is EventType.TERMINATION:
            reason = payload.get("termination_reason")
            termination_reason = reason if isinstance(reason, str) else None
            status = payload.get("completion_status")
            completion_status = status if isinstance(status, str) and status != "none" else None
            files = payload.get("changed_files")
            if isinstance(files, list):
                changed_files = [item for item in files if isinstance(item, str)]
        elif event_type is EventType.ROLLBACK:
            # A rollback trace has no task text; the run it undid is what names it.
            target = payload.get("target_run_id")
            target_run_id = target if isinstance(target, str) else None
    return {
        "task": task,
        "termination_reason": termination_reason,
        "completion_status": completion_status,
        "changed_files": changed_files,
        "target_run_id": target_run_id,
    }


def _offerable_directory(item: Path) -> bool:
    """Hide the caches and runtime directories the file tools already ignore."""

    if item.name.casefold() in DEFAULT_IGNORED_DIRECTORIES:
        return False
    try:
        return item.is_dir()
    except OSError:
        return False


def _filesystem_roots(cwd: Path) -> tuple[Path, ...]:
    """Return the short list of starting points offered by the directory picker."""

    roots: list[Path] = []
    home = Path.home()
    if home.is_dir():
        roots.append(home)
    anchor = Path(cwd.anchor) if cwd.anchor else None
    if anchor is not None and anchor.is_dir() and anchor not in roots:
        roots.append(anchor)
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if drive.is_dir() and drive not in roots:
                roots.append(drive)
    return tuple(roots)


def _all_ok(checks: Sequence[Mapping[str, object]]) -> bool:
    return all(bool(check.get("ok")) for check in checks)


def _session_error_status(code: str) -> int:
    if code == "RUN_NOT_FOUND":
        return 404
    if code in {"WORKSPACE_BUSY", "TOO_MANY_ACTIVE_RUNS", "DUPLICATE_RUN_ID"}:
        return 409
    return 400


def _parse_cursor(value: str | None) -> int:
    if value is None or value == "":
        return 0
    try:
        cursor = int(value)
    except ValueError:
        raise ValueError("cursor must be an integer") from None
    if cursor < 0:
        raise ValueError("cursor must not be negative")
    return cursor


def _parse_wait(value: str | None) -> float:
    if value is None or value == "":
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError("wait must be a number of seconds") from None
    if not 0 <= seconds <= MAX_WAIT_SECONDS:
        raise ValueError(f"wait must be between 0 and {MAX_WAIT_SECONDS:g} seconds")
    return seconds


def _parse_limit(value: str | None) -> int:
    if value is None or value == "":
        return DEFAULT_HISTORY_ENTRIES
    try:
        limit = int(value)
    except ValueError:
        raise ValueError("limit must be an integer") from None
    if not 1 <= limit <= MAX_HISTORY_ENTRIES:
        raise ValueError(f"limit must be between 1 and {MAX_HISTORY_ENTRIES}")
    return limit


def _parse_limits(body: Mapping[str, object]) -> AgentRunLimits:
    """Apply the same bounds the command line enforces on run overrides."""

    return AgentRunLimits(
        max_steps=_bounded_int(
            body.get("max_steps"),
            label="max_steps",
            default=DEFAULT_LIMITS.max_steps,
            minimum=1,
            maximum=MAX_AGENT_STEPS,
        ),
        max_seconds=_bounded_float(
            body.get("max_seconds"),
            label="max_seconds",
            default=DEFAULT_LIMITS.max_seconds,
            minimum=1.0,
            maximum=MAX_AGENT_SECONDS,
        ),
        context_budget_bytes=_bounded_int(
            body.get("context_budget_bytes"),
            label="context_budget_bytes",
            default=DEFAULT_CONTEXT_BUDGET_BYTES,
            minimum=MIN_CONTEXT_BUDGET_BYTES,
            maximum=MAX_CONTEXT_BUDGET_BYTES,
        ),
        max_consecutive_failures=_bounded_int(
            body.get("max_consecutive_failures"),
            label="max_consecutive_failures",
            default=DEFAULT_LIMITS.max_consecutive_failures,
            minimum=1,
            maximum=MAX_CONSECUTIVE_FAILURES,
        ),
        max_api_attempts=_bounded_int(
            body.get("max_api_attempts"),
            label="max_api_attempts",
            default=DEFAULT_LIMITS.max_api_attempts,
            minimum=1,
            maximum=DEFAULT_MAX_API_ATTEMPTS,
        ),
    )


def _bounded_int(value: object, *, label: str, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | str | float):
        raise ValueError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an integer") from None
    if isinstance(value, float) and parsed != value:
        raise ValueError(f"{label} must be an integer")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(
    value: object,
    *,
    label: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"{label} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return parsed
