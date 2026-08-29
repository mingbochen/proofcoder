"""Deterministic offline scanning of repository secrets across Git evidence surfaces.

The scanner intentionally reports locations and rule identifiers only.  Matched
content is never retained in public result objects or rendered output.
"""

from __future__ import annotations

import bisect
import codecs
import hashlib
import json
import os
import re
import stat
import subprocess
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

SCHEMA_VERSION = 1
DISCLAIMER = (
    "Secret scanning is pattern-based and cannot prove the absolute absence of every form "
    "of secret."
)
REMEDIATION = (
    "Immediately revoke and rotate affected credentials at the service provider; do not "
    "continue using exposed credentials. Preserve the real published Git history required "
    "by the applicable rules and obtain human review before any history remediation."
)

_SCOPE_ORDER = {"working_tree": 0, "index": 1, "history": 2}
_HEX_OID = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_PRIVATE_KEY_PATTERN = re.compile(
    re.escape("-" * 5)
    + r"BEGIN\s+(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED)\s+)?"
    + "PRIVATE"
    + r"\s+"
    + "KEY"
    + re.escape("-" * 5),
    re.IGNORECASE,
)
_KNOWN_CREDENTIAL_PATTERNS = (
    (
        "content.openai_api_key",
        re.compile(r"\bsk" + re.escape("-") + r"[A-Za-z0-9_-]{16,}\b"),
        "OpenAI-style API key prefix",
    ),
    (
        "content.github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        "GitHub token prefix",
    ),
    (
        "content.aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        "AWS access key identifier",
    ),
    (
        "content.google_api_key",
        re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b"),
        "Google API key prefix",
    ),
    (
        "content.slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        "Slack token prefix",
    ),
    (
        "content.stripe_live_key",
        re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
        "Stripe live credential prefix",
    ),
)
_AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)\s+"
    r"(?P<credential>[A-Za-z0-9+/=_-]{8,})"
)
_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)\b(?P<name>[A-Za-z][A-Za-z0-9_.-]{1,80})(?P<name_quote>[\"']?)"
    r"[\t ]*(?:(?<![=:])=(?!=)|:(?!=))[\t ]*"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|"
    r"\{[A-Za-z_][A-Za-z0-9_]*\}|\[[A-Za-z_][A-Za-z0-9_-]*\]|"
    r"[^\s#;,\r\n]+)"
)
_CREDENTIAL_URL_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://"
    r"(?P<username>[^\s/:@]+):(?P<password>[^\s/@]+)@"
)
_SENSITIVE_VARIABLE_SUFFIXES = (
    "API_KEY",
    "ACCESS_KEY",
    "AUTH_TOKEN",
    "CLIENT_SECRET",
    "CREDENTIAL",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
)
_PLACEHOLDER_WORD = re.compile(
    r"(?i)(?:"
    r"change[-_ ]?me|replace[-_ ]?me|"
    r"(?:your|example|sample|dummy|fake|test|fictional|obviously[-_ ]?fake)"
    r"(?:[-_ ][a-z0-9]+)*|not[-_ ]a[-_ ]secret|redacted"
    r")"
)
_ENV_REFERENCE = re.compile(
    r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|"
    r"\$env:[A-Za-z_][A-Za-z0-9_]*|"
    r"%[A-Za-z_][A-Za-z0-9_]*%|"
    r"\{[A-Za-z_][A-Za-z0-9_]*\}|"
    r"\{\{[A-Za-z_][A-Za-z0-9_]*\}\})",
    re.IGNORECASE,
)
_TEMPLATE_ANGLE = re.compile(r"<(?=[^>]*(?:your|key|token|secret|value))[^>]+>", re.IGNORECASE)
_REFERENCE_IDENTIFIER = re.compile(
    r"(?:FAKE|DUMMY|TEST|EXAMPLE)_[A-Z0-9_]+|[A-Z][A-Z0-9_]*_SENTINEL"
)
_CODE_REFERENCE = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*")
_NON_SECRET_LITERAL = frozenset(
    {
        "bool",
        "bytes",
        "dict",
        "false",
        "float",
        "int",
        "list",
        "none",
        "null",
        "object",
        "str",
        "true",
        "tuple",
    }
)
_SENSITIVE_EXACT_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "_netrc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "service-account.json",
        "service_account.json",
    }
)
_PRIVATE_CONTAINER_SUFFIXES = frozenset({".jks", ".key", ".keystore", ".p12", ".pfx"})
_ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})
_WORKTREE_EXCLUDED_COMPONENTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".proofcoder",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
)


class ScanScope(StrEnum):
    """Public evidence surfaces supported by the scanner."""

    WORKING_TREE = "working_tree"
    INDEX = "index"
    HISTORY = "history"


DEFAULT_SCOPES = (ScanScope.WORKING_TREE, ScanScope.INDEX, ScanScope.HISTORY)


@dataclass(frozen=True, slots=True)
class ScanLimits:
    """Explicit resource limits used by all evidence surfaces."""

    max_content_bytes: int = 1024 * 1024
    max_candidates: int = 20_000
    max_findings: int = 1_000
    max_git_output_bytes: int = 64 * 1024 * 1024
    max_history_paths: int = 100_000
    git_timeout_seconds: int = 60


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """Safe location metadata for one finding, without matched content."""

    finding_id: str
    rule_id: str
    scope: ScanScope
    path: str
    severity: str
    description: str
    line: int | None = None
    column: int | None = None
    blob_oid_prefix: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "scope": self.scope.value,
            "path": self.path,
            "severity": self.severity,
            "description": self.description,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        if self.blob_oid_prefix is not None:
            result["blob_oid_prefix"] = self.blob_oid_prefix
        return result


@dataclass(frozen=True, slots=True)
class ScanIssue:
    """Stable safe error or warning metadata."""

    code: str
    message: str
    scope: ScanScope | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": self.message}
        if self.scope is not None:
            result["scope"] = self.scope.value
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True, slots=True)
class ScopeStatistics:
    """Deterministic counters for one scan scope."""

    candidates: int = 0
    scanned: int = 0
    skipped_binary: int = 0
    findings: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "candidates": self.candidates,
            "scanned": self.scanned,
            "skipped_binary": self.skipped_binary,
            "findings": self.findings,
        }


@dataclass(frozen=True, slots=True)
class SecretScanReport:
    """Complete deterministic report for one repository state."""

    scopes: dict[ScanScope, ScopeStatistics]
    findings: tuple[SecretFinding, ...]
    errors: tuple[ScanIssue, ...]
    warnings: tuple[ScanIssue, ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def scan_complete(self) -> bool:
        return not self.errors

    @property
    def automatic_pass(self) -> bool:
        return self.scan_complete and not self.findings

    @property
    def exit_code(self) -> int:
        if not self.scan_complete:
            return 2
        return 1 if self.findings else 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scan_complete": self.scan_complete,
            "automatic_pass": self.automatic_pass,
            "scopes": {
                scope.value: statistics.to_dict()
                for scope, statistics in sorted(
                    self.scopes.items(), key=lambda item: _SCOPE_ORDER[item[0].value]
                )
            },
            "summary": {
                "candidates": sum(item.candidates for item in self.scopes.values()),
                "scanned": sum(item.scanned for item in self.scopes.values()),
                "skipped_binary": sum(item.skipped_binary for item in self.scopes.values()),
                "findings": len(self.findings),
                "errors": len(self.errors),
                "warnings": len(self.warnings),
            },
            "findings": [finding.to_dict() for finding in self.findings],
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "disclaimer": DISCLAIMER,
        }


class SecretScanInfrastructureError(Exception):
    """A bounded failure that prevents a trustworthy complete scan."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        scope: ScanScope | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.scope = scope
        self.path = path


@dataclass(slots=True)
class _MutableStatistics:
    candidates: int = 0
    scanned: int = 0
    skipped_binary: int = 0


@dataclass(frozen=True, slots=True)
class _ContentMatch:
    rule_id: str
    line: int
    column: int
    description: str


@dataclass(slots=True)
class _ScanState:
    limits: ScanLimits
    statistics: dict[ScanScope, _MutableStatistics]
    findings: list[SecretFinding] = field(default_factory=list)
    errors: list[ScanIssue] = field(default_factory=list)
    warnings: list[ScanIssue] = field(default_factory=list)
    _finding_keys: set[tuple[object, ...]] = field(default_factory=set)
    _issue_keys: set[tuple[object, ...]] = field(default_factory=set)

    def add_finding(
        self,
        rule_id: str,
        scope: ScanScope,
        path: str,
        description: str,
        *,
        line: int | None = None,
        column: int | None = None,
        oid: str | None = None,
    ) -> None:
        key = (scope.value, path, line, column, rule_id, oid)
        if key in self._finding_keys:
            return
        if len(self.findings) >= self.limits.max_findings:
            self.add_error(
                "FINDING_LIMIT_EXCEEDED",
                "The finding limit was exceeded; the report is incomplete.",
                scope=scope,
            )
            return
        self._finding_keys.add(key)
        identifier_input = "|".join(
            (
                scope.value,
                path,
                "" if line is None else str(line),
                "" if column is None else str(column),
                rule_id,
                "" if oid is None else oid,
            )
        )
        finding_id = hashlib.sha256(identifier_input.encode("utf-8")).hexdigest()[:16]
        self.findings.append(
            SecretFinding(
                finding_id=finding_id,
                rule_id=rule_id,
                scope=scope,
                path=path,
                severity="error",
                description=description,
                line=line,
                column=column,
                blob_oid_prefix=None if oid is None else oid[:12],
            )
        )

    def add_error(
        self,
        code: str,
        message: str,
        *,
        scope: ScanScope | None = None,
        path: str | None = None,
    ) -> None:
        self._add_issue(self.errors, code, message, scope=scope, path=path)

    def add_warning(
        self,
        code: str,
        message: str,
        *,
        scope: ScanScope | None = None,
        path: str | None = None,
    ) -> None:
        self._add_issue(self.warnings, code, message, scope=scope, path=path)

    def _add_issue(
        self,
        target: list[ScanIssue],
        code: str,
        message: str,
        *,
        scope: ScanScope | None,
        path: str | None,
    ) -> None:
        key = ("error" if target is self.errors else "warning", code, scope, path)
        if key in self._issue_keys:
            return
        self._issue_keys.add(key)
        target.append(ScanIssue(code=code, message=message, scope=scope, path=path))


def scan_repository(
    root: Path,
    *,
    scopes: Sequence[ScanScope] = DEFAULT_SCOPES,
    limits: ScanLimits | None = None,
) -> SecretScanReport:
    """Scan selected repository evidence surfaces without network or mutation."""

    selected = _normalize_scopes(scopes)
    active_limits = ScanLimits() if limits is None else limits
    statistics = {scope: _MutableStatistics() for scope in selected}
    state = _ScanState(active_limits, statistics)
    try:
        repository = _resolve_repository_root(root, active_limits)
    except SecretScanInfrastructureError as error:
        state.add_error(error.code, str(error), scope=error.scope, path=error.path)
        return _build_report(state)

    for scope in selected:
        try:
            if scope is ScanScope.WORKING_TREE:
                _scan_working_tree(repository, state)
            elif scope is ScanScope.INDEX:
                _scan_index(repository, state)
            else:
                _scan_history(repository, state)
        except SecretScanInfrastructureError as error:
            state.add_error(
                error.code,
                str(error),
                scope=error.scope or scope,
                path=error.path,
            )
    return _build_report(state)


def infrastructure_error_report(
    scopes: Sequence[ScanScope],
    code: str,
    message: str,
) -> SecretScanReport:
    """Build the fixed report schema for a safe top-level CLI failure."""

    selected = _normalize_scopes(scopes)
    state = _ScanState(ScanLimits(), {scope: _MutableStatistics() for scope in selected})
    state.add_error(code, message)
    return _build_report(state)


def format_json(report: SecretScanReport) -> str:
    """Render a deterministic UTF-8 JSON report."""

    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"


def format_text(report: SecretScanReport) -> str:
    """Render a deterministic report containing safe metadata only."""

    lines = ["ProofCoder repository secret scan"]
    for scope, statistics in sorted(
        report.scopes.items(), key=lambda item: _SCOPE_ORDER[item[0].value]
    ):
        lines.append(
            f"{scope.value}: candidates={statistics.candidates} scanned={statistics.scanned} "
            f"skipped_binary={statistics.skipped_binary} findings={statistics.findings}"
        )
    for finding in report.findings:
        location = finding.path
        if finding.line is not None:
            location += f":{finding.line}"
            if finding.column is not None:
                location += f":{finding.column}"
        oid = "" if finding.blob_oid_prefix is None else f" blob={finding.blob_oid_prefix}"
        lines.append(f"FINDING {finding.rule_id} scope={finding.scope.value} path={location}{oid}")
    for issue in report.errors:
        lines.append(_format_issue("ERROR", issue))
    for issue in report.warnings:
        lines.append(_format_issue("WARNING", issue))
    lines.extend(
        (
            f"findings: {len(report.findings)}",
            f"errors: {len(report.errors)}",
            f"warnings: {len(report.warnings)}",
            f"scan complete: {str(report.scan_complete).lower()}",
            f"automatic pass: {str(report.automatic_pass).lower()}",
        )
    )
    if report.findings:
        lines.append(f"Remediation: {REMEDIATION}")
    lines.append(f"Disclaimer: {DISCLAIMER}")
    return "\n".join(lines) + "\n"


def _format_issue(label: str, issue: ScanIssue) -> str:
    details = []
    if issue.scope is not None:
        details.append(f"scope={issue.scope.value}")
    if issue.path is not None:
        details.append(f"path={issue.path}")
    suffix = "" if not details else " " + " ".join(details)
    return f"{label} {issue.code}{suffix}: {issue.message}"


def _normalize_scopes(scopes: Sequence[ScanScope]) -> tuple[ScanScope, ...]:
    selected = tuple(sorted(set(scopes), key=lambda scope: _SCOPE_ORDER[scope.value]))
    if not selected:
        raise ValueError("at least one scan scope is required")
    return selected


def _build_report(state: _ScanState) -> SecretScanReport:
    findings = tuple(sorted(state.findings, key=_finding_sort_key))
    errors = tuple(sorted(state.errors, key=_issue_sort_key))
    warnings = tuple(sorted(state.warnings, key=_issue_sort_key))
    counts = defaultdict(int)
    for finding in findings:
        counts[finding.scope] += 1
    scopes = {
        scope: ScopeStatistics(
            candidates=mutable.candidates,
            scanned=mutable.scanned,
            skipped_binary=mutable.skipped_binary,
            findings=counts[scope],
        )
        for scope, mutable in sorted(
            state.statistics.items(), key=lambda item: _SCOPE_ORDER[item[0].value]
        )
    }
    return SecretScanReport(scopes, findings, errors, warnings)


def _finding_sort_key(finding: SecretFinding) -> tuple[object, ...]:
    return (
        _SCOPE_ORDER[finding.scope.value],
        finding.path.casefold(),
        finding.path,
        0 if finding.line is None else finding.line,
        0 if finding.column is None else finding.column,
        finding.rule_id,
        "" if finding.blob_oid_prefix is None else finding.blob_oid_prefix,
    )


def _issue_sort_key(issue: ScanIssue) -> tuple[object, ...]:
    return (
        -1 if issue.scope is None else _SCOPE_ORDER[issue.scope.value],
        "" if issue.path is None else issue.path.casefold(),
        "" if issue.path is None else issue.path,
        issue.code,
    )


def _git_environment() -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
    environment = {name: os.environ[name] for name in os.environ if name.upper() in allowed}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _resolve_repository_root(root: Path, limits: ScanLimits) -> Path:
    try:
        candidate = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SecretScanInfrastructureError(
            "ROOT_INVALID", "The requested repository root is not an accessible directory."
        ) from None
    if not candidate.is_dir():
        raise SecretScanInfrastructureError(
            "ROOT_INVALID", "The requested repository root is not an accessible directory."
        )
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=candidate,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=limits.git_timeout_seconds,
        )
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed."
        ) from None
    except (OSError, subprocess.TimeoutExpired):
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git could not validate the repository root."
        ) from None
    if completed.returncode != 0:
        raise SecretScanInfrastructureError(
            "NOT_A_GIT_REPOSITORY", "The requested root is not a Git repository."
        )
    try:
        discovered = Path(completed.stdout.decode("utf-8").strip()).resolve(strict=True)
    except (OSError, RuntimeError, UnicodeDecodeError):
        raise SecretScanInfrastructureError(
            "GIT_OUTPUT_ERROR", "Git returned an invalid repository root."
        ) from None
    if discovered != candidate:
        raise SecretScanInfrastructureError(
            "ROOT_NOT_TOPLEVEL", "The requested root must be the Git repository top level."
        )
    return candidate


def _git_working_tree_paths(root: Path, limits: ScanLimits) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=limits.git_timeout_seconds,
        )
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed."
        ) from None
    except (OSError, subprocess.TimeoutExpired):
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git could not enumerate working-tree paths."
        ) from None
    return _validated_git_output(completed, limits, "working-tree paths")


def _git_index_entries(root: Path, limits: ScanLimits) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=limits.git_timeout_seconds,
        )
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed."
        ) from None
    except (OSError, subprocess.TimeoutExpired):
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git could not enumerate index entries."
        ) from None
    return _validated_git_output(completed, limits, "index entries")


def _git_history_objects(root: Path, limits: ScanLimits) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "rev-list", "--objects", "--all", "-z"],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=limits.git_timeout_seconds,
        )
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed."
        ) from None
    except (OSError, subprocess.TimeoutExpired):
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git could not enumerate reachable history objects."
        ) from None
    return _validated_git_output(completed, limits, "history objects")


def _git_history_changes(root: Path, limits: ScanLimits) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "log", "--all", "--format=", "--raw", "-z", "--no-renames", "--no-abbrev"],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
            timeout=limits.git_timeout_seconds,
        )
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed."
        ) from None
    except (OSError, subprocess.TimeoutExpired):
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git could not enumerate history paths."
        ) from None
    return _validated_git_output(completed, limits, "history paths")


def _validated_git_output(
    completed: subprocess.CompletedProcess[bytes],
    limits: ScanLimits,
    description: str,
) -> bytes:
    if completed.returncode != 0:
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", f"Git could not enumerate {description}."
        )
    if len(completed.stdout) > limits.max_git_output_bytes:
        raise SecretScanInfrastructureError(
            "GIT_OUTPUT_LIMIT_EXCEEDED",
            f"Git {description} output exceeded the configured limit.",
        )
    return completed.stdout


def _scan_working_tree(root: Path, state: _ScanState) -> None:
    scope = ScanScope.WORKING_TREE
    raw_paths = _git_working_tree_paths(root, state.limits)
    paths = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        path = _decode_and_normalize_path(raw_path, scope)
        if _working_tree_path_excluded(path):
            continue
        paths.append(path)
    paths = sorted(set(paths), key=_path_sort_key)
    if len(paths) > state.limits.max_candidates:
        raise SecretScanInfrastructureError(
            "CANDIDATE_LIMIT_EXCEEDED",
            "The working-tree candidate limit was exceeded.",
            scope=scope,
        )
    state.statistics[scope].candidates = len(paths)
    for path in paths:
        if _is_sensitive_repository_path(path):
            state.add_finding(
                "path.sensitive",
                scope,
                path,
                "A sensitive credential or private-key path is present in Git-visible files.",
            )
            continue
        candidate = root.joinpath(*PurePosixPath(path).parts)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            state.add_error(
                "FILE_INSPECTION_ERROR",
                "A working-tree path could not be safely inspected.",
                scope=scope,
                path=path,
            )
            continue
        if stat.S_ISLNK(metadata.st_mode) or _has_symlink_component(root, path):
            state.add_error(
                "SYMLINK_REJECTED",
                "A working-tree symlink was not followed; the scan is incomplete.",
                scope=scope,
                path=path,
            )
            continue
        try:
            candidate.resolve(strict=False).relative_to(root)
        except (OSError, RuntimeError, ValueError):
            state.add_error(
                "PATH_OUTSIDE_REPOSITORY",
                "A working-tree path resolves outside the repository.",
                scope=scope,
                path=path,
            )
            continue
        if not stat.S_ISREG(metadata.st_mode):
            state.add_error(
                "SPECIAL_FILE_REJECTED",
                "A working-tree path is not a regular file.",
                scope=scope,
                path=path,
            )
            continue
        if metadata.st_size > state.limits.max_content_bytes:
            state.add_error(
                "CONTENT_LIMIT_EXCEEDED",
                "A working-tree file exceeded the configured content limit.",
                scope=scope,
                path=path,
            )
            continue
        try:
            with candidate.open("rb") as stream:
                content = stream.read(state.limits.max_content_bytes + 1)
        except OSError:
            state.add_error(
                "FILE_READ_ERROR",
                "A working-tree file could not be read.",
                scope=scope,
                path=path,
            )
            continue
        if len(content) > state.limits.max_content_bytes:
            state.add_error(
                "CONTENT_LIMIT_EXCEEDED",
                "A working-tree file exceeded the configured content limit.",
                scope=scope,
                path=path,
            )
            continue
        _scan_content(content, (path,), scope, state)


def _scan_index(root: Path, state: _ScanState) -> None:
    scope = ScanScope.INDEX
    raw = _git_index_entries(root, state.limits)
    entries: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, raw_oid, _stage = metadata.split(b" ", 2)
            oid = raw_oid.decode("ascii")
        except (ValueError, UnicodeDecodeError):
            raise SecretScanInfrastructureError(
                "GIT_OUTPUT_ERROR", "Git returned malformed index metadata.", scope=scope
            ) from None
        if _HEX_OID.fullmatch(raw_oid) is None:
            raise SecretScanInfrastructureError(
                "GIT_OUTPUT_ERROR", "Git returned an invalid index object identifier.", scope=scope
            )
        entries.append(
            (
                mode.decode("ascii", errors="ignore"),
                oid,
                _decode_and_normalize_path(raw_path, scope),
            )
        )
    entries.sort(key=lambda item: (*_path_sort_key(item[2]), item[1], item[0]))
    if len(entries) > state.limits.max_candidates:
        raise SecretScanInfrastructureError(
            "CANDIDATE_LIMIT_EXCEEDED", "The index candidate limit was exceeded.", scope=scope
        )
    state.statistics[scope].candidates = len(entries)
    paths_by_oid: dict[str, set[str]] = defaultdict(set)
    for mode, oid, path in entries:
        if mode == "120000":
            state.add_error(
                "SYMLINK_REJECTED",
                "An index symlink was not followed; the scan is incomplete.",
                scope=scope,
                path=path,
            )
            continue
        if _is_sensitive_repository_path(path):
            state.add_finding(
                "path.sensitive",
                scope,
                path,
                "A sensitive credential or private-key path is present in the Git index.",
                oid=oid,
            )
            continue
        paths_by_oid[oid].add(path)
    _scan_git_blobs(root, paths_by_oid, scope, state)


def _scan_history(root: Path, state: _ScanState) -> None:
    scope = ScanScope.HISTORY
    raw_objects = _git_history_objects(root, state.limits)
    raw_changes = _git_history_changes(root, state.limits)
    paths_by_oid: dict[str, set[str]] = defaultdict(set)
    object_ids: set[str] = set()
    object_records = [record for record in raw_objects.split(b"\0") if record]
    object_index = 0
    while object_index < len(object_records):
        raw_oid = object_records[object_index]
        if _HEX_OID.fullmatch(raw_oid) is None:
            raise SecretScanInfrastructureError(
                "GIT_OUTPUT_ERROR", "Git returned invalid history object metadata.", scope=scope
            )
        oid = raw_oid.decode("ascii")
        object_ids.add(oid)
        object_index += 1
        if (
            object_index < len(object_records)
            and _HEX_OID.fullmatch(object_records[object_index]) is None
        ):
            raw_path = object_records[object_index]
            if raw_path.startswith(b"path="):
                raw_path = raw_path[len(b"path=") :]
            paths_by_oid[oid].add(_decode_and_normalize_path(raw_path, scope))
            object_index += 1
    chunks = raw_changes.split(b"\0")
    index = 0
    while index + 1 < len(chunks):
        header = chunks[index].lstrip(b"\r\n")
        if not header.startswith(b":"):
            index += 1
            continue
        path = _decode_and_normalize_path(chunks[index + 1], scope)
        for raw_oid in _HEX_OID.findall(header):
            if set(raw_oid) == {ord("0")}:
                continue
            oid = raw_oid.decode("ascii")
            object_ids.add(oid)
            paths_by_oid[oid].add(path)
        index += 2
    if len(object_ids) > state.limits.max_candidates:
        raise SecretScanInfrastructureError(
            "CANDIDATE_LIMIT_EXCEEDED",
            "The reachable history object limit was exceeded.",
            scope=scope,
        )
    if sum(len(paths) for paths in paths_by_oid.values()) > state.limits.max_history_paths:
        raise SecretScanInfrastructureError(
            "HISTORY_PATH_LIMIT_EXCEEDED",
            "The reachable history path limit was exceeded.",
            scope=scope,
        )
    metadata = _batch_check_objects(root, sorted(object_ids), state.limits, scope)
    blob_ids = {oid for oid, (kind, _size) in metadata.items() if kind == "blob"}
    state.statistics[scope].candidates = len(blob_ids)
    blob_paths: dict[str, set[str]] = {}
    for oid in sorted(blob_ids):
        paths = paths_by_oid.get(oid, set())
        if not paths:
            state.add_error(
                "HISTORY_PATH_UNAVAILABLE",
                "A reachable history blob has no safe repository path location.",
                scope=scope,
            )
            continue
        for path in sorted(paths, key=_path_sort_key):
            if _is_sensitive_repository_path(path):
                state.add_finding(
                    "path.sensitive",
                    scope,
                    path,
                    "A sensitive credential or private-key path is reachable in Git history.",
                    oid=oid,
                )
        safe_paths = {path for path in paths if not _is_sensitive_repository_path(path)}
        if safe_paths:
            blob_paths[oid] = safe_paths
    _scan_git_blobs(root, blob_paths, scope, state, metadata=metadata)


def _batch_check_objects(
    root: Path,
    object_ids: Sequence[str],
    limits: ScanLimits,
    scope: ScanScope,
) -> dict[str, tuple[str, int]]:
    if not object_ids:
        return {}
    payload = ("\n".join(object_ids) + "\n").encode("ascii")
    try:
        process = subprocess.Popen(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        stdout, _ = process.communicate(payload, timeout=limits.git_timeout_seconds)
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed.", scope=scope
        ) from None
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git object inspection timed out.", scope=scope
        ) from None
    except OSError:
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git could not inspect repository objects.", scope=scope
        ) from None
    if process.returncode != 0 or len(stdout) > limits.max_git_output_bytes:
        raise SecretScanInfrastructureError(
            "GIT_OBJECT_ERROR", "Git could not safely inspect all repository objects.", scope=scope
        )
    result: dict[str, tuple[str, int]] = {}
    for line in stdout.splitlines():
        fields = line.split(b" ")
        if len(fields) != 3 or _HEX_OID.fullmatch(fields[0]) is None:
            raise SecretScanInfrastructureError(
                "GIT_OBJECT_ERROR", "Git returned malformed object metadata.", scope=scope
            )
        try:
            oid = fields[0].decode("ascii")
            kind = fields[1].decode("ascii")
            size = int(fields[2])
        except (UnicodeDecodeError, ValueError):
            raise SecretScanInfrastructureError(
                "GIT_OBJECT_ERROR", "Git returned malformed object metadata.", scope=scope
            ) from None
        result[oid] = (kind, size)
    if set(result) != set(object_ids):
        raise SecretScanInfrastructureError(
            "GIT_OBJECT_ERROR", "Git did not return every requested repository object.", scope=scope
        )
    return result


def _scan_git_blobs(
    root: Path,
    paths_by_oid: dict[str, set[str]],
    scope: ScanScope,
    state: _ScanState,
    *,
    metadata: dict[str, tuple[str, int]] | None = None,
) -> None:
    if not paths_by_oid:
        return
    object_ids = sorted(paths_by_oid)
    object_metadata = (
        _batch_check_objects(root, object_ids, state.limits, scope)
        if metadata is None
        else metadata
    )
    eligible: list[tuple[str, int]] = []
    for oid in object_ids:
        kind, size = object_metadata[oid]
        if kind != "blob":
            state.add_warning(
                "NON_BLOB_SKIPPED",
                "A non-blob Git entry has no file content to scan.",
                scope=scope,
            )
            continue
        if size > state.limits.max_content_bytes:
            state.add_error(
                "CONTENT_LIMIT_EXCEEDED",
                "A Git blob exceeded the configured content limit.",
                scope=scope,
                path=sorted(paths_by_oid[oid], key=_path_sort_key)[0],
            )
            continue
        eligible.append((oid, size))
    if not eligible:
        return
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=root,
            env=_git_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        if process.stdin is None or process.stdout is None:
            raise OSError
        for oid, expected_size in eligible:
            process.stdin.write(oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline(512)
            fields = header.rstrip(b"\n").split(b" ")
            if (
                len(fields) != 3
                or fields[0].decode("ascii", errors="ignore") != oid
                or fields[1] != b"blob"
            ):
                raise SecretScanInfrastructureError(
                    "GIT_OBJECT_ERROR",
                    "Git returned malformed blob content metadata.",
                    scope=scope,
                )
            try:
                returned_size = int(fields[2])
            except ValueError:
                raise SecretScanInfrastructureError(
                    "GIT_OBJECT_ERROR",
                    "Git returned malformed blob content metadata.",
                    scope=scope,
                ) from None
            if returned_size != expected_size or returned_size > state.limits.max_content_bytes:
                raise SecretScanInfrastructureError(
                    "GIT_OBJECT_ERROR",
                    "Git blob size changed during scanning.",
                    scope=scope,
                )
            content = _read_exact(process.stdout, returned_size)
            if process.stdout.read(1) != b"\n":
                raise SecretScanInfrastructureError(
                    "GIT_OBJECT_ERROR", "Git returned truncated blob content.", scope=scope
                )
            _scan_content(
                content,
                tuple(sorted(paths_by_oid[oid], key=_path_sort_key)),
                scope,
                state,
                oid=oid,
            )
        process.stdin.close()
        process.wait(timeout=state.limits.git_timeout_seconds)
        if process.returncode != 0:
            raise SecretScanInfrastructureError(
                "GIT_OBJECT_ERROR", "Git could not read every requested blob.", scope=scope
            )
    except FileNotFoundError:
        raise SecretScanInfrastructureError(
            "GIT_NOT_AVAILABLE", "Git is unavailable; the scan cannot be completed.", scope=scope
        ) from None
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.wait()
        raise SecretScanInfrastructureError(
            "GIT_COMMAND_ERROR", "Git blob reading timed out.", scope=scope
        ) from None
    except OSError:
        if process is not None:
            process.kill()
            process.wait()
        raise SecretScanInfrastructureError(
            "GIT_OBJECT_ERROR", "Git could not safely read repository blobs.", scope=scope
        ) from None
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise SecretScanInfrastructureError(
                "GIT_OBJECT_ERROR", "Git returned truncated blob content."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _scan_content(
    content: bytes,
    paths: Sequence[str],
    scope: ScanScope,
    state: _ScanState,
    *,
    oid: str | None = None,
) -> None:
    if b"\0" in content:
        state.statistics[scope].skipped_binary += 1
        return
    try:
        text = content.decode("utf-8-sig" if content.startswith(codecs.BOM_UTF8) else "utf-8")
    except UnicodeDecodeError:
        state.add_error(
            "TEXT_DECODE_ERROR",
            "A non-binary file or blob is not valid UTF-8 text.",
            scope=scope,
            path=paths[0],
        )
        return
    state.statistics[scope].scanned += 1
    for match in _find_content_matches(text):
        for path in paths:
            state.add_finding(
                match.rule_id,
                scope,
                path,
                match.description,
                line=match.line,
                column=match.column,
                oid=oid,
            )


def _find_content_matches(text: str) -> tuple[_ContentMatch, ...]:
    offsets: list[tuple[str, int, str]] = []
    for match in _PRIVATE_KEY_PATTERN.finditer(text):
        offsets.append(("content.private_key", match.start(), "Private-key header"))
    for rule_id, pattern, description in _KNOWN_CREDENTIAL_PATTERNS:
        offsets.extend((rule_id, match.start(), description) for match in pattern.finditer(text))
    for match in _AUTHORIZATION_PATTERN.finditer(text):
        credential = match.group("credential")
        if not _is_placeholder(credential):
            offsets.append(
                (
                    "content.authorization_credential",
                    match.start("credential"),
                    "Concrete Authorization credential",
                )
            )
    for match in _ASSIGNMENT_PATTERN.finditer(text):
        if not _is_sensitive_variable_name(match.group("name")):
            continue
        if not _is_assignment_placeholder(
            match.group("name"), match.group("name_quote"), match.group("value")
        ):
            offsets.append(
                (
                    "content.sensitive_assignment",
                    match.start("value"),
                    "Concrete sensitive-variable assignment",
                )
            )
    for match in _CREDENTIAL_URL_PATTERN.finditer(text):
        if not _is_placeholder(match.group("password")):
            offsets.append(
                (
                    "content.credential_url",
                    match.start("password"),
                    "URL containing explicit user credentials",
                )
            )
    line_starts = [0]
    line_starts.extend(match.end() for match in re.finditer("\n", text))
    unique = set(offsets)
    results = []
    for rule_id, offset, description in sorted(unique, key=lambda item: (item[1], item[0])):
        line_index = bisect.bisect_right(line_starts, offset) - 1
        results.append(
            _ContentMatch(
                rule_id=rule_id,
                line=line_index + 1,
                column=offset - line_starts[line_index] + 1,
                description=description,
            )
        )
    return tuple(results)


def _is_sensitive_variable_name(name: str) -> bool:
    normalized = re.sub(r"[-.]", "_", name).upper()
    return any(
        normalized == suffix or normalized.endswith("_" + suffix)
        for suffix in _SENSITIVE_VARIABLE_SUFFIXES
    )


def _is_placeholder(raw_value: str) -> bool:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        value = value[1:-1].strip()
    if not value:
        return True
    if value in {"[]", "{}", "[redacted]"}:
        return True
    if value.isdecimal():
        return True
    if value.casefold() in _NON_SECRET_LITERAL:
        return True
    return bool(
        _ENV_REFERENCE.fullmatch(value)
        or _TEMPLATE_ANGLE.fullmatch(value)
        or _PLACEHOLDER_WORD.fullmatch(value)
        or _REFERENCE_IDENTIFIER.fullmatch(value)
        or _CODE_REFERENCE.fullmatch(value)
    )


def _is_assignment_placeholder(name: str, name_quote: str, raw_value: str) -> bool:
    if _is_placeholder(raw_value):
        return True
    value = raw_value.strip()
    unwrapped = value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        unwrapped = value[1:-1].strip()
        if any(marker in unwrapped for marker in (",", "=", "(")):
            return True
    syntactic_value = unwrapped.rstrip(")]}")
    if syntactic_value != unwrapped and _is_placeholder(syntactic_value):
        return True
    normalized = syntactic_value.casefold().replace("_", "-").replace(" ", "-")
    if normalized.startswith(("do-not-", "must-not-", "never-", "obviously-fake-", "placeholder-")):
        return True
    if "-must-not-" in normalized and re.fullmatch(r"[a-z]+(?:-[a-z]+)+", normalized):
        return True
    if normalized.endswith(("-for-test", "-for-tests", "-test-only")):
        return True
    if "(" in unwrapped:
        return True
    if unwrapped.isidentifier() and any(character.islower() for character in name):
        return True
    return bool(name_quote and unwrapped.isidentifier() and unwrapped.isupper())


def _is_sensitive_repository_path(path: str) -> bool:
    return any(_is_sensitive_component(part) for part in PurePosixPath(path).parts)


def _is_sensitive_component(component: str) -> bool:
    lowered = component.casefold()
    if lowered in _ENV_TEMPLATE_NAMES:
        return False
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered in _SENSITIVE_EXACT_NAMES:
        return True
    suffix = Path(lowered).suffix
    if suffix in _PRIVATE_CONTAINER_SUFFIXES:
        return True
    if suffix == ".pem":
        stem = Path(lowered).stem
        normalized = stem.replace("-", "_")
        return (
            stem in {"key", "private", "privatekey"}
            or "private_key" in normalized
            or normalized.endswith("_key")
        )
    return False


def _working_tree_path_excluded(path: str) -> bool:
    parts = {part.casefold() for part in PurePosixPath(path).parts}
    return bool(parts & _WORKTREE_EXCLUDED_COMPONENTS) or path.casefold().endswith((".pyc", ".pyo"))


def _has_symlink_component(root: Path, path: str) -> bool:
    current = root
    for part in PurePosixPath(path).parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False


def _decode_and_normalize_path(raw_path: bytes, scope: ScanScope) -> str:
    try:
        decoded = raw_path.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretScanInfrastructureError(
            "PATH_DECODE_ERROR", "Git returned a path that is not valid UTF-8.", scope=scope
        ) from None
    normalized = decoded.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    windows = PureWindowsPath(decoded)
    if (
        not normalized
        or candidate.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise SecretScanInfrastructureError(
            "PATH_INVALID", "Git returned an unsafe repository path.", scope=scope
        )
    return candidate.as_posix()


def _path_sort_key(path: str) -> tuple[str, str]:
    return path.casefold(), path
