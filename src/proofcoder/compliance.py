"""Deterministic offline repository compliance evidence.

The checks in this module are conservative static checks.  They provide
repeatable mechanical evidence, not a formal proof or an absolute security
guarantee.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import stat
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

SCHEMA_VERSION = 1
MAX_CHECKED_FILE_BYTES = 2 * 1024 * 1024
DISCLAIMER = "Mechanical checks cannot replace manual dependency and call-chain review."

_DISTRIBUTION_SEPARATOR = re.compile(r"[-_.]+")
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_SAFE_CHECK_ID_COMPONENT = re.compile(r"[^a-z0-9]+")

_SPECIFICATION_PATH = "docs/DEVELOPMENT_SPEC.md"
_README_PATH = "README.md"
_ROADMAP_PATH = "docs/ROADMAP.md"
_ADR_DIRECTORY = "docs/adr"
_ADR_INDEX_PATH = "docs/adr/README.md"
_ADR_TEMPLATE_NAME = "0000-template.md"
_TOOL_REGISTRATION_PATH = "src/proofcoder/agent_runtime.py"
_TOOL_DEFINITION_NAMES = frozenset({"ToolDefinition", "base.ToolDefinition"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPECIFICATION_TOOL_HEADING = re.compile(r"### 7\.\d+ `([^`]+)`\s*")
_README_TOOL_SECTION = "## Local Tools"
_README_TOOL_ROW = re.compile(r"\|\s*`([^`]+)`\s*\|.*")
_ADR_CANDIDATE_NAME = re.compile(r"\d{4}-.*\.md")
_ADR_FILE_NAME = re.compile(r"(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md")
# Chinese headers use a full-width colon (U+FF1A). It is written as an escape because
# ruff flags the literal character as confusable with an ASCII colon.
_ADR_TITLE = re.compile(r"# ADR-(\d{4})\uff1a.+")
_ADR_STATUS_PREFIX = "- 状态\uff1a"
_ADR_STATUSES = frozenset({"提议", "已接受", "已拒绝"})
_ADR_SUPERSEDED_STATUS = re.compile(r"已被 ADR-\d{4} 取代")
_ADR_INDEX_HEADER = "编号"
_ADR_INDEX_LINK = re.compile(r"\[(\d{4})\]\(([^()\s]+)\)")
_ADR_HEADER_SCAN_LINES = 20
_ROADMAP_STATUS_HEADER = "状态"
_ROADMAP_PR_HEADER = "PR"
_ROADMAP_STATUSES = frozenset({"已完成", "进行中", "未开始", "暂缓"})
_ROADMAP_BLOCKED_STATUS_PREFIX = "阻塞"
_ROADMAP_COMPLETED_STATUS = "已完成"
_EMPTY_TABLE_CELLS = frozenset({"", "—", "-"})
_TABLE_SEPARATOR = re.compile(r"\|(?:\s*:?-+:?\s*\|)+\s*")
_LAYOUT_HEADING_PREFIX = "### 5.1"
# Tree lines are built from U+2502, U+251C, U+2514, and U+2500 box-drawing characters.
_LAYOUT_ENTRY = re.compile(r"((?:│   |    )*)(?:├── |└── )(.+?)\s*")
_LAYOUT_NAME = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?")


class CheckStatus(StrEnum):
    """Stable status values used by every compliance record."""

    PASS = "pass"
    FAIL = "fail"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ComplianceCheck:
    """One bounded, source-content-free compliance observation."""

    check_id: str
    status: CheckStatus
    message: str
    path: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible mapping with a fixed key order."""

        result: dict[str, object] = {
            "check_id": self.check_id,
            "status": self.status.value,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.line is not None:
            result["line"] = self.line
        return result


@dataclass(frozen=True, slots=True)
class ComplianceReport:
    """A complete deterministic repository compliance report."""

    checks: tuple[ComplianceCheck, ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def automatic_pass(self) -> bool:
        """Return whether no definite automatic violation was found."""

        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    @property
    def manual_review(self) -> tuple[ComplianceCheck, ...]:
        """Return checks whose static evidence is intentionally inconclusive."""

        return tuple(check for check in self.checks if check.status is CheckStatus.REVIEW)

    @property
    def counts(self) -> dict[str, int]:
        """Return counts in the public PASS, FAIL, REVIEW order."""

        return {
            "pass": sum(check.status is CheckStatus.PASS for check in self.checks),
            "fail": sum(check.status is CheckStatus.FAIL for check in self.checks),
            "review": sum(check.status is CheckStatus.REVIEW for check in self.checks),
        }

    def to_dict(self) -> dict[str, object]:
        """Return the fixed JSON schema without absolute machine paths."""

        return {
            "schema_version": self.schema_version,
            "automatic_pass": self.automatic_pass,
            "summary": self.counts,
            "checks": [check.to_dict() for check in self.checks],
            "manual_review": [check.to_dict() for check in self.manual_review],
            "disclaimer": DISCLAIMER,
        }


class ComplianceInfrastructureError(Exception):
    """A stable repository or checker failure that prevents trustworthy results."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _ParsedPythonFile:
    path: str
    tree: ast.Module


@dataclass(frozen=True, slots=True)
class _Capability:
    check_id: str
    description: str
    implementations: tuple[tuple[str, tuple[str, ...]], ...]
    tests: tuple[str, ...]
    require_shell_false: bool = False


_FORBIDDEN_DISTRIBUTIONS = frozenset(
    {
        "agents",
        "anthropic-agent-sdk",
        "autogen",
        "claude-agent-sdk",
        "claude-code",
        "claude-code-sdk",
        "codex",
        "codex-cli",
        "crewai",
        "deepseek-harness",
        "openai-agents",
        "openai-codex",
        "opencode",
        "opencode-ai",
        "opencode-sdk",
        "pyautogen",
    }
)
_FORBIDDEN_DISTRIBUTION_FAMILIES = (
    "autogen-",
    "claude-agent-sdk-",
    "claude-code-",
    "codex-",
    "crewai-",
    "deepseek-harness-",
    "langchain-",
    "llama-index-",
    "openai-agents-",
    "opencode-",
)
_FORBIDDEN_DISTRIBUTION_FAMILY_ROOTS = frozenset({"langchain", "llama-index"})

_FORBIDDEN_IMPORTS = frozenset(
    {
        "agents",
        "anthropic_agent_sdk",
        "autogen",
        "claude_agent_sdk",
        "claude_code",
        "codex",
        "crewai",
        "deepseek_harness",
        "opencode",
        "pyautogen",
    }
)
_FORBIDDEN_IMPORT_PREFIXES = (
    "autogen_",
    "claude_agent_sdk_",
    "claude_code_",
    "codex_",
    "crewai_",
    "deepseek_harness_",
    "langchain_",
    "llama_index_",
    "opencode_",
)
_FORBIDDEN_IMPORT_ROOTS = frozenset({"langchain", "llama_index"})

_SUBPROCESS_FUNCTIONS = frozenset({"run", "popen", "call", "check_call", "check_output"})
_OS_COMMAND_FUNCTIONS = frozenset({"system", "popen"})
_FORBIDDEN_AGENT_COMMANDS = frozenset(
    {
        "claude",
        "claude-code",
        "codex",
        "deepseek-harness",
        "opencode",
    }
)
_COMMAND_WRAPPERS = frozenset({"bunx", "npm", "npx", "pipx", "pnpm", "uvx", "yarn"})

_HOSTED_TOOL_TYPES = frozenset(
    {
        "apply_patch",
        "code_interpreter",
        "computer_use",
        "computer_use_preview",
        "file_search",
        "hosted_apply_patch",
        "hosted_shell",
        "local_shell",
        "shell",
    }
)
_FILES_API_OPERATIONS = frozenset(
    {"create", "delete", "list", "retrieve", "retrieve_content", "wait_for_processing"}
)

_CAPABILITIES = (
    _Capability(
        "capability.deepseek_client",
        "DeepSeek API client",
        (("src/proofcoder/llm/deepseek.py", ("DeepSeekClient",)),),
        ("tests/unit/test_deepseek.py",),
    ),
    _Capability(
        "capability.message_history_context",
        "MessageHistory and context compaction",
        (("src/proofcoder/context.py", ("MessageHistory", "ContextManager")),),
        ("tests/unit/test_context.py", "tests/unit/test_context_manager.py"),
    ),
    _Capability(
        "capability.agent_loop",
        "AgentLoop",
        (("src/proofcoder/agent.py", ("AgentLoop",)),),
        ("tests/unit/test_agent.py", "tests/unit/test_agent_d2.py"),
    ),
    _Capability(
        "capability.tool_registry_validation",
        "ToolRegistry and local argument validation",
        (("src/proofcoder/tools/registry.py", ("ToolRegistry", "_validate_arguments")),),
        ("tests/unit/test_tools.py",),
    ),
    _Capability(
        "capability.local_file_tools",
        "local file reading, search, and exact editing",
        (
            ("src/proofcoder/tools/files.py", ("create_read_file_tool",)),
            ("src/proofcoder/tools/search.py", ("create_search_text_tool",)),
            (
                "src/proofcoder/tools/edit.py",
                ("create_create_file_tool", "create_replace_in_file_tool"),
            ),
        ),
        (
            "tests/unit/test_read_file.py",
            "tests/unit/test_search_text.py",
            "tests/unit/test_edit_tools.py",
        ),
    ),
    _Capability(
        "capability.command_policy",
        "command policy and shell=False execution",
        (
            ("src/proofcoder/safety/commands.py", ("prepare_command",)),
            ("src/proofcoder/tools/command.py", ("create_run_command_tool",)),
        ),
        ("tests/unit/test_command_policy.py", "tests/unit/test_run_command.py"),
        require_shell_false=True,
    ),
    _Capability(
        "capability.api_retry",
        "API retry policy",
        (
            ("src/proofcoder/retry.py", ("retry_delay_seconds",)),
            ("src/proofcoder/agent.py", ("_request_model",)),
        ),
        ("tests/unit/test_retry.py", "tests/unit/test_agent_d2.py"),
    ),
    _Capability(
        "capability.no_progress_termination",
        "no-progress and termination control",
        (
            ("src/proofcoder/progress.py", ("NoProgressTracker",)),
            ("src/proofcoder/agent.py", ("AgentLoop",)),
        ),
        ("tests/unit/test_progress.py", "tests/unit/test_agent_d2.py"),
    ),
    _Capability(
        "capability.verification_finish",
        "VerificationTracker and finish_task",
        (
            ("src/proofcoder/verification.py", ("VerificationTracker",)),
            (
                "src/proofcoder/tools/finish.py",
                ("create_finish_task_tool", "build_finish_outcome"),
            ),
        ),
        ("tests/unit/test_verification.py", "tests/unit/test_finish_task.py"),
    ),
    _Capability(
        "capability.checkpoint_rollback",
        "run checkpoints and rollback",
        (
            (
                "src/proofcoder/checkpoint.py",
                ("create_checkpoint", "plan_rollback", "apply_rollback"),
            ),
        ),
        ("tests/unit/test_checkpoint.py", "tests/unit/test_checkpoint_runtime.py"),
    ),
    _Capability(
        "capability.trace_replay",
        "events, trace, and replay",
        (
            ("src/proofcoder/events.py", ("RunEvent", "EventEmitter")),
            ("src/proofcoder/trace.py", ("TraceRecorder", "read_trace")),
        ),
        ("tests/unit/test_events.py", "tests/unit/test_trace.py"),
    ),
    _Capability(
        "capability.eval_pipeline",
        "eval fixture, core, and persistent runner",
        (
            ("src/proofcoder/eval_fixtures.py", ("EvalFixture", "load_fixtures")),
            ("src/proofcoder/eval_core.py", ("run_evaluation_attempt",)),
            ("src/proofcoder/eval_runner.py", ("run_evaluation",)),
        ),
        (
            "tests/unit/test_eval_fixtures.py",
            "tests/unit/test_eval_core.py",
            "tests/unit/test_eval_runner.py",
        ),
    ),
)


def normalize_distribution_name(name: str) -> str:
    """Normalize a Python distribution name using PEP 503 equivalence."""

    return _DISTRIBUTION_SEPARATOR.sub("-", name).casefold()


def run_compliance(root: Path) -> ComplianceReport:
    """Run every offline check against one repository root."""

    repository = _resolve_repository_root(root)
    parsed_files = _parse_python_sources(repository)
    checks = [
        *check_dependencies(repository),
        *check_python_sources(parsed_files),
        *check_capability_evidence(repository),
        *check_documentation_consistency(repository),
    ]
    return ComplianceReport(checks=tuple(sorted(checks, key=_check_sort_key)))


def check_dependencies(root: Path) -> tuple[ComplianceCheck, ...]:
    """Check structured direct, optional, grouped, and locked dependencies."""

    repository = _resolve_repository_root(root)
    pyproject = _read_toml(repository, "pyproject.toml")
    lock = _read_toml(repository, "uv.lock")
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ComplianceInfrastructureError(
            "PYPROJECT_STRUCTURE_ERROR", "pyproject.toml has no [project] table"
        )

    scopes: list[tuple[str, str, tuple[str, ...]]] = []
    scopes.append(
        (
            "dependency.pyproject.runtime",
            "pyproject.toml runtime dependencies",
            _requirement_names(project.get("dependencies", []), "project.dependencies"),
        )
    )

    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ComplianceInfrastructureError(
            "PYPROJECT_STRUCTURE_ERROR", "project.optional-dependencies must be a table"
        )
    optional_names: list[str] = []
    for group_name, requirements in sorted(optional.items()):
        optional_names.extend(
            _requirement_names(requirements, f"project.optional-dependencies.{group_name}")
        )
    scopes.append(
        (
            "dependency.pyproject.optional",
            "pyproject.toml optional dependencies",
            tuple(optional_names),
        )
    )

    dependency_groups = pyproject.get("dependency-groups", {})
    if not isinstance(dependency_groups, dict):
        raise ComplianceInfrastructureError(
            "PYPROJECT_STRUCTURE_ERROR", "dependency-groups must be a table"
        )
    group_names: list[str] = []
    for group_name, entries in sorted(dependency_groups.items()):
        group_names.extend(_dependency_group_names(entries, str(group_name)))
    scopes.append(
        (
            "dependency.pyproject.groups",
            "pyproject.toml dependency groups",
            tuple(group_names),
        )
    )

    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ComplianceInfrastructureError(
            "UV_LOCK_STRUCTURE_ERROR", "uv.lock package entries must be an array"
        )
    locked_names: list[str] = []
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ComplianceInfrastructureError(
                "UV_LOCK_STRUCTURE_ERROR", "uv.lock contains an invalid package entry"
            )
        locked_names.append(str(package["name"]))
    scopes.append(
        (
            "dependency.uv_lock",
            "uv.lock packages",
            tuple(locked_names),
        )
    )

    checks: list[ComplianceCheck] = []
    for check_id, description, names in scopes:
        violations = sorted(
            {normalize_distribution_name(name) for name in names if _forbidden_distribution(name)}
        )
        if not violations:
            checks.append(
                ComplianceCheck(
                    check_id,
                    CheckStatus.PASS,
                    (
                        f"{description} contain no listed forbidden distributions "
                        f"({len(names)} checked)"
                    ),
                    "uv.lock" if check_id == "dependency.uv_lock" else "pyproject.toml",
                )
            )
            continue
        for name in violations:
            checks.append(
                ComplianceCheck(
                    f"{check_id}.forbidden.{_id_component(name)}",
                    CheckStatus.FAIL,
                    f"forbidden distribution is declared or locked: {name}",
                    "uv.lock" if check_id == "dependency.uv_lock" else "pyproject.toml",
                )
            )
    return tuple(checks)


def check_python_sources(
    parsed_files: Sequence[_ParsedPythonFile],
) -> tuple[ComplianceCheck, ...]:
    """Run import, subprocess, and hosted-tool checks over parsed Python files."""

    checks = [
        *_check_imports(parsed_files),
        *_check_subprocesses(parsed_files),
        *_check_hosted_tools(parsed_files),
    ]
    return tuple(checks)


def check_capability_evidence(root: Path) -> tuple[ComplianceCheck, ...]:
    """Check deterministic implementation-symbol-test evidence mappings."""

    repository = _resolve_repository_root(root)
    checks: list[ComplianceCheck] = []
    for capability in _CAPABILITIES:
        failures: list[str] = []
        primary_path = capability.implementations[0][0]
        parsed_by_path: dict[str, ast.Module] = {}
        for relative, symbols in capability.implementations:
            try:
                source = _read_text_file(repository, relative)
                tree = ast.parse(source, filename=relative)
            except (ComplianceInfrastructureError, SyntaxError):
                failures.append(f"invalid implementation file: {relative}")
                continue
            parsed_by_path[relative] = tree
            available = _defined_symbols(tree)
            missing_symbols = sorted(set(symbols) - available)
            if missing_symbols:
                failures.append(f"missing symbols in {relative}: {', '.join(missing_symbols)}")

        for relative in capability.tests:
            if not _is_regular_repository_file(repository, relative):
                failures.append(f"missing regular test file: {relative}")

        if capability.require_shell_false and not any(
            _has_shell_false_call(tree) for tree in parsed_by_path.values()
        ):
            failures.append("no subprocess call with explicit shell=False was found")

        if failures:
            checks.append(
                ComplianceCheck(
                    capability.check_id,
                    CheckStatus.FAIL,
                    f"{capability.description}: {'; '.join(failures)}",
                    primary_path,
                )
            )
        else:
            implementation_paths = ", ".join(path for path, _ in capability.implementations)
            test_paths = ", ".join(capability.tests)
            checks.append(
                ComplianceCheck(
                    capability.check_id,
                    CheckStatus.PASS,
                    (
                        f"{capability.description}: implementation symbols found in "
                        f"{implementation_paths}; coverage files present: {test_paths}"
                    ),
                    primary_path,
                )
            )
    return tuple(checks)


def check_documentation_consistency(root: Path) -> tuple[ComplianceCheck, ...]:
    """Check structural agreement between code, the specification, and project documents.

    These checks compare names, paths, and status values only. They cannot judge whether
    prose is accurate, so a pass means the documents agree in structure, not in meaning.
    """

    repository = _resolve_repository_root(root)
    return (
        *_check_documented_tools(repository),
        *_check_adr_records(repository),
        *_check_roadmap_statuses(repository),
        *_check_specification_layout(repository),
    )


def format_json(report: ComplianceReport) -> str:
    """Serialize a report as deterministic, parseable JSON."""

    return json.dumps(
        report.to_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def format_text(report: ComplianceReport) -> str:
    """Render deterministic human-readable output without source bodies."""

    lines: list[str] = []
    for check in report.checks:
        location = ""
        if check.path is not None:
            location = f" [{check.path}"
            if check.line is not None:
                location += f":{check.line}"
            location += "]"
        lines.append(f"{check.status.value.upper()} {check.check_id}{location}: {check.message}")
    counts = report.counts
    lines.append(f"Summary: PASS={counts['pass']} FAIL={counts['fail']} REVIEW={counts['review']}")
    lines.append(f"Automatic checks: {'PASS' if report.automatic_pass else 'FAIL'}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def infrastructure_error_json(error: ComplianceInfrastructureError) -> str:
    """Serialize one safe infrastructure error using the report schema version."""

    return json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "automatic_pass": False,
            "summary": {"pass": 0, "fail": 0, "review": 0},
            "checks": [],
            "manual_review": [],
            "infrastructure_error": {"code": error.code, "message": str(error)},
            "disclaimer": DISCLAIMER,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _resolve_repository_root(root: Path) -> Path:
    if root.is_symlink():
        raise ComplianceInfrastructureError(
            "ROOT_SYMLINK", "repository root must be an ordinary directory, not a symlink"
        )
    try:
        resolved = root.resolve(strict=True)
        metadata = root.stat(follow_symlinks=False)
    except OSError:
        raise ComplianceInfrastructureError(
            "ROOT_INVALID", "repository root does not exist or cannot be inspected"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode) or not resolved.is_dir():
        raise ComplianceInfrastructureError(
            "ROOT_INVALID", "repository root must be an ordinary directory"
        )
    return resolved


def _read_toml(root: Path, relative: str) -> dict[str, object]:
    text = _read_text_file(root, relative)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ComplianceInfrastructureError(
            "TOML_PARSE_ERROR", f"{relative} is not valid TOML"
        ) from None
    return parsed


def _read_text_file(root: Path, relative: str) -> str:
    path = _repository_path(root, relative)
    if path.is_symlink():
        raise ComplianceInfrastructureError(
            "SYMLINK_REJECTED", f"repository file is a symlink: {relative}"
        )
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise ComplianceInfrastructureError(
            "FILE_UNREADABLE", f"required repository file cannot be inspected: {relative}"
        ) from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ComplianceInfrastructureError(
            "SPECIAL_FILE_REJECTED", f"repository path is not a regular file: {relative}"
        )
    if metadata.st_size > MAX_CHECKED_FILE_BYTES:
        raise ComplianceInfrastructureError(
            "FILE_TOO_LARGE", f"repository file exceeds the size limit: {relative}"
        )
    try:
        raw = path.read_bytes()
    except OSError:
        raise ComplianceInfrastructureError(
            "FILE_UNREADABLE", f"repository file cannot be read: {relative}"
        ) from None
    if len(raw) > MAX_CHECKED_FILE_BYTES:
        raise ComplianceInfrastructureError(
            "FILE_TOO_LARGE", f"repository file exceeds the size limit: {relative}"
        )
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ComplianceInfrastructureError(
            "FILE_DECODE_ERROR", f"repository file is not UTF-8 text: {relative}"
        ) from None


def _repository_path(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or candidate.is_absolute()
        or PureWindowsPath(relative).drive
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ComplianceInfrastructureError(
            "PATH_INVALID", "checker-internal repository path is invalid"
        )
    path = root.joinpath(*candidate.parts)
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        raise ComplianceInfrastructureError(
            "PATH_OUTSIDE_ROOT", f"repository path resolves outside the root: {relative}"
        ) from None
    return path


def _is_regular_repository_file(root: Path, relative: str) -> bool:
    try:
        path = _repository_path(root, relative)
        if path.is_symlink():
            return False
        metadata = path.stat(follow_symlinks=False)
        path.resolve(strict=True).relative_to(root)
    except (ComplianceInfrastructureError, OSError, ValueError):
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_CHECKED_FILE_BYTES


def _parse_python_sources(root: Path) -> tuple[_ParsedPythonFile, ...]:
    relative_paths = [
        *_discover_python_tree(root, "src/proofcoder", recursive=True),
        *_discover_python_tree(root, "scripts", recursive=False),
    ]
    parsed: list[_ParsedPythonFile] = []
    for relative in sorted(set(relative_paths)):
        source = _read_text_file(root, relative)
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as error:
            line = error.lineno if isinstance(error.lineno, int) else None
            location = relative if line is None else f"{relative}:{line}"
            raise ComplianceInfrastructureError(
                "PYTHON_PARSE_ERROR", f"Python syntax error prevents checking: {location}"
            ) from None
        parsed.append(_ParsedPythonFile(relative, tree))
    return tuple(parsed)


def _discover_python_tree(root: Path, relative: str, *, recursive: bool) -> tuple[str, ...]:
    directory = _repository_path(root, relative)
    if not directory.exists():
        return ()
    if directory.is_symlink():
        raise ComplianceInfrastructureError(
            "SYMLINK_REJECTED", f"Python source directory is a symlink: {relative}"
        )
    try:
        metadata = directory.stat(follow_symlinks=False)
    except OSError:
        raise ComplianceInfrastructureError(
            "FILE_UNREADABLE", f"Python source directory cannot be inspected: {relative}"
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise ComplianceInfrastructureError(
            "SPECIAL_FILE_REJECTED", f"Python source root is not a directory: {relative}"
        )

    found: list[str] = []

    def visit(current: Path, prefix: str) -> None:
        try:
            entries = sorted(
                os.scandir(current),
                key=lambda entry: (entry.name.casefold(), entry.name),
            )
        except OSError:
            raise ComplianceInfrastructureError(
                "FILE_UNREADABLE", f"Python source directory cannot be listed: {prefix}"
            ) from None
        for entry in entries:
            child_relative = f"{prefix}/{entry.name}"
            try:
                if entry.is_symlink():
                    raise ComplianceInfrastructureError(
                        "SYMLINK_REJECTED",
                        f"Python source path is a symlink: {child_relative}",
                    )
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        visit(Path(entry.path), child_relative)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise ComplianceInfrastructureError(
                        "SPECIAL_FILE_REJECTED",
                        f"Python source path is not regular: {child_relative}",
                    )
            except OSError:
                raise ComplianceInfrastructureError(
                    "FILE_UNREADABLE", f"Python source path cannot be inspected: {child_relative}"
                ) from None
            if entry.name.endswith(".py"):
                found.append(child_relative)

    visit(directory, relative)
    return tuple(found)


def _requirement_names(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ComplianceInfrastructureError(
            "PYPROJECT_STRUCTURE_ERROR", f"{location} must be an array"
        )
    names: list[str] = []
    for requirement in value:
        if not isinstance(requirement, str):
            raise ComplianceInfrastructureError(
                "PYPROJECT_STRUCTURE_ERROR", f"{location} contains a non-string requirement"
            )
        match = _REQUIREMENT_NAME.match(requirement)
        if match is None:
            raise ComplianceInfrastructureError(
                "PYPROJECT_STRUCTURE_ERROR", f"{location} contains an invalid requirement"
            )
        names.append(match.group(1))
    return tuple(names)


def _dependency_group_names(value: object, group_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ComplianceInfrastructureError(
            "PYPROJECT_STRUCTURE_ERROR", f"dependency group {group_name} must be an array"
        )
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            names.extend(_requirement_names([entry], f"dependency-groups.{group_name}"))
        elif (
            isinstance(entry, dict)
            and set(entry) == {"include-group"}
            and isinstance(entry["include-group"], str)
        ):
            continue
        else:
            raise ComplianceInfrastructureError(
                "PYPROJECT_STRUCTURE_ERROR",
                f"dependency group {group_name} contains an invalid entry",
            )
    return tuple(names)


def _forbidden_distribution(name: str) -> bool:
    normalized = normalize_distribution_name(name)
    return (
        normalized in _FORBIDDEN_DISTRIBUTIONS
        or normalized in _FORBIDDEN_DISTRIBUTION_FAMILY_ROOTS
        or normalized.startswith(_FORBIDDEN_DISTRIBUTION_FAMILIES)
    )


def _check_imports(parsed_files: Sequence[_ParsedPythonFile]) -> tuple[ComplianceCheck, ...]:
    failures: list[ComplianceCheck] = []
    for parsed in parsed_files:
        for node in ast.walk(parsed.tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                modules.append(node.module)
            for module in modules:
                if _forbidden_import(module):
                    failures.append(
                        ComplianceCheck(
                            "python.import.forbidden",
                            CheckStatus.FAIL,
                            f"forbidden framework or agent product import: {module}",
                            parsed.path,
                            node.lineno,
                        )
                    )
    if failures:
        return tuple(failures)
    return (
        ComplianceCheck(
            "python.imports",
            CheckStatus.PASS,
            f"no forbidden imports found in {len(parsed_files)} Python files",
        ),
    )


def _forbidden_import(module: str) -> bool:
    normalized = module.casefold().replace("-", "_")
    root = normalized.split(".", 1)[0]
    return (
        root in _FORBIDDEN_IMPORTS
        or root in _FORBIDDEN_IMPORT_ROOTS
        or root.startswith(_FORBIDDEN_IMPORT_PREFIXES)
    )


def _check_subprocesses(
    parsed_files: Sequence[_ParsedPythonFile],
) -> tuple[ComplianceCheck, ...]:
    records: list[ComplianceCheck] = []
    inspected_calls = 0
    for parsed in parsed_files:
        aliases = _command_call_aliases(parsed.tree)
        for node in ast.walk(parsed.tree):
            if not isinstance(node, ast.Call):
                continue
            command_kind = _command_call_kind(node.func, aliases)
            if command_kind is None:
                continue
            inspected_calls += 1
            argument = _call_argument(node, "args" if command_kind == "subprocess" else "command")
            argv = _static_command_argv(argument)
            if argv is None:
                records.append(
                    ComplianceCheck(
                        "python.subprocess.dynamic",
                        CheckStatus.REVIEW,
                        "command target is dynamically constructed and requires call-chain review",
                        parsed.path,
                        node.lineno,
                    )
                )
            elif _forbidden_agent_command(argv):
                records.append(
                    ComplianceCheck(
                        "python.subprocess.forbidden_agent",
                        CheckStatus.FAIL,
                        f"subprocess explicitly starts a forbidden agent product: {argv[0]}",
                        parsed.path,
                        node.lineno,
                    )
                )
    if not any(record.status is CheckStatus.FAIL for record in records):
        records.append(
            ComplianceCheck(
                "python.subprocesses",
                CheckStatus.PASS,
                (
                    "no statically resolved subprocess starts a listed agent product "
                    f"({inspected_calls} calls inspected)"
                ),
            )
        )
    return tuple(records)


def _command_call_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"os", "subprocess"}:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"os", "subprocess"}:
            for alias in node.names:
                imported_name = alias.name.casefold()
                if node.module == "subprocess" and imported_name in _SUBPROCESS_FUNCTIONS:
                    aliases[alias.asname or alias.name] = f"subprocess.{imported_name}"
                if node.module == "os" and imported_name in _OS_COMMAND_FUNCTIONS:
                    aliases[alias.asname or alias.name] = f"os.{imported_name}"
    return aliases


def _command_call_kind(func: ast.expr, aliases: Mapping[str, str]) -> str | None:
    dotted = _dotted_name(func)
    if dotted is None:
        return None
    parts = dotted.split(".")
    replacement = aliases.get(parts[0])
    if replacement is not None:
        parts = [*replacement.split("."), *parts[1:]]
    parts = [part.casefold() for part in parts]
    if len(parts) == 2 and parts[0] == "subprocess" and parts[1] in _SUBPROCESS_FUNCTIONS:
        return "subprocess"
    if len(parts) == 2 and parts[0] == "os" and parts[1] in _OS_COMMAND_FUNCTIONS:
        return "os"
    return None


def _call_argument(call: ast.Call, keyword_name: str) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg in {keyword_name, "args"}:
            return keyword.value
    return None


def _static_command_argv(node: ast.expr | None) -> tuple[str, ...] | None:
    if node is None:
        return None
    value = _literal_value(node)
    if isinstance(value, str):
        try:
            parts = shlex.split(value, posix=True)
        except ValueError:
            return None
        return tuple(parts) if parts else None
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) for item in value):
        return tuple(value)
    return None


def _literal_value(node: ast.expr) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _forbidden_agent_command(argv: Sequence[str]) -> bool:
    normalized = [_command_token(item) for item in argv]
    if not normalized:
        return False
    executable = normalized[0]
    if executable in _FORBIDDEN_AGENT_COMMANDS:
        return True
    if executable in {"python", "python3", "py"} and len(normalized) >= 3:
        return normalized[1] == "-m" and normalized[2] in _FORBIDDEN_AGENT_COMMANDS
    if executable in _COMMAND_WRAPPERS:
        return any(token in _FORBIDDEN_AGENT_COMMANDS for token in normalized[1:])
    return False


def _command_token(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".sh"):
        name = name.removesuffix(suffix)
    return name.replace("_", "-")


def _check_hosted_tools(
    parsed_files: Sequence[_ParsedPythonFile],
) -> tuple[ComplianceCheck, ...]:
    records: list[ComplianceCheck] = []
    for parsed in parsed_files:
        for node in ast.walk(parsed.tree):
            if isinstance(node, ast.Dict):
                tool_type = _dict_string_value(node, "type")
                if tool_type is not None and tool_type.casefold() in _HOSTED_TOOL_TYPES:
                    records.append(
                        ComplianceCheck(
                            "python.hosted_tool.schema",
                            CheckStatus.FAIL,
                            f"forbidden hosted tool schema type: {tool_type}",
                            parsed.path,
                            node.lineno,
                        )
                    )
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted_name(node.func)
            lowered = () if dotted is None else tuple(part.casefold() for part in dotted.split("."))
            if _is_files_api_call(lowered):
                records.append(
                    ComplianceCheck(
                        "python.hosted_tool.files_api",
                        CheckStatus.FAIL,
                        "forbidden Files API or vector-store call",
                        parsed.path,
                        node.lineno,
                    )
                )
            elif any(keyword.arg == "file_id" for keyword in node.keywords):
                records.append(
                    ComplianceCheck(
                        "python.hosted_tool.file_id",
                        CheckStatus.FAIL,
                        "API call passes a server-hosted file_id",
                        parsed.path,
                        node.lineno,
                    )
                )
            elif _is_provider_create_call(lowered) and any(
                keyword.arg is None for keyword in node.keywords
            ):
                records.append(
                    ComplianceCheck(
                        "python.hosted_tool.dynamic_request",
                        CheckStatus.REVIEW,
                        (
                            "provider request uses dynamic keyword expansion; "
                            "review the request data flow"
                        ),
                        parsed.path,
                        node.lineno,
                    )
                )
    if not any(record.status is CheckStatus.FAIL for record in records):
        records.append(
            ComplianceCheck(
                "python.hosted_tools",
                CheckStatus.PASS,
                "no explicit hosted file, search, or execution tool declaration was found",
            )
        )
    return tuple(records)


def _dict_string_value(node: ast.Dict, key_name: str) -> str | None:
    for key, value in zip(node.keys, node.values, strict=True):
        if (
            isinstance(key, ast.Constant)
            and key.value == key_name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _is_files_api_call(parts: Sequence[str]) -> bool:
    if not parts:
        return False
    if "vector_stores" in parts or "vectorstores" in parts:
        return True
    return len(parts) >= 2 and parts[-2] == "files" and parts[-1] in _FILES_API_OPERATIONS


def _is_provider_create_call(parts: Sequence[str]) -> bool:
    return (len(parts) >= 3 and tuple(parts[-3:]) == ("chat", "completions", "create")) or (
        len(parts) >= 2 and tuple(parts[-2:]) == ("responses", "create")
    )


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def _defined_symbols(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _has_shell_false_call(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if (
                keyword.arg == "shell"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
            ):
                return True
    return False


def _optional_text(root: Path, relative: str) -> str | None:
    try:
        return _read_text_file(root, relative)
    except ComplianceInfrastructureError:
        return None


def _missing_document(check_id: str, relative: str) -> ComplianceCheck:
    return ComplianceCheck(
        check_id,
        CheckStatus.FAIL,
        f"required document is missing, not a regular file, or unreadable: {relative}",
        relative,
    )


def _check_documented_tools(root: Path) -> tuple[ComplianceCheck, ...]:
    check_id = "documentation.tools"
    registered, problems = _registered_tool_names(root, check_id)
    if problems:
        return problems

    failures: list[ComplianceCheck] = []
    specification = _optional_text(root, _SPECIFICATION_PATH)
    if specification is None:
        failures.append(_missing_document(check_id, _SPECIFICATION_PATH))
    else:
        failures.extend(
            _compare_documented_tools(
                check_id,
                registered,
                _specification_tool_names(specification),
                _SPECIFICATION_PATH,
                "specification section 7",
            )
        )

    readme = _optional_text(root, _README_PATH)
    if readme is None:
        failures.append(_missing_document(check_id, _README_PATH))
    else:
        readme_tools = _readme_tool_names(readme)
        if readme_tools is None:
            failures.append(
                ComplianceCheck(
                    check_id,
                    CheckStatus.FAIL,
                    f"README has no '{_README_TOOL_SECTION}' section",
                    _README_PATH,
                )
            )
        else:
            failures.extend(
                _compare_documented_tools(
                    check_id, registered, readme_tools, _README_PATH, "the README tool table"
                )
            )

    if failures:
        return tuple(failures)
    return (
        ComplianceCheck(
            check_id,
            CheckStatus.PASS,
            (
                f"{len(registered)} registered tools match specification section 7 "
                "and the README tool table"
            ),
            _TOOL_REGISTRATION_PATH,
        ),
    )


def _registered_tool_names(
    root: Path, check_id: str
) -> tuple[tuple[str, ...], tuple[ComplianceCheck, ...]]:
    """Resolve registered tool names from source, without importing project code."""

    source = _optional_text(root, _TOOL_REGISTRATION_PATH)
    if source is None:
        return (), (_missing_document(check_id, _TOOL_REGISTRATION_PATH),)
    try:
        tree = ast.parse(source, filename=_TOOL_REGISTRATION_PATH)
    except SyntaxError:
        return (), (_tool_failure(check_id, "tool registration module is not valid Python"),)

    imports: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module, alias.name)

    factories: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
        ):
            factories.append((node.args[0].func.id, node.lineno))
    if not factories:
        return (), (_tool_failure(check_id, "no tool registrations were found"),)

    names: list[str] = []
    problems: list[ComplianceCheck] = []
    for factory, line in sorted(factories, key=lambda item: item[1]):
        imported = imports.get(factory)
        if imported is None or not imported[0].startswith("proofcoder."):
            problems.append(
                _tool_failure(
                    check_id,
                    f"tool factory is not imported from a proofcoder module: {factory}",
                    line,
                )
            )
            continue
        name = _tool_definition_name(root, imported[0], imported[1])
        if name is None:
            message = f"tool name cannot be resolved statically for factory: {factory}"
        elif _IDENTIFIER.fullmatch(name) is None:
            message = f"tool name is not a plain identifier for factory: {factory}"
        elif name in names:
            message = f"tool name is registered more than once: {name}"
        else:
            names.append(name)
            continue
        problems.append(_tool_failure(check_id, message, line))
    return tuple(names), tuple(problems)


def _tool_failure(check_id: str, message: str, line: int | None = None) -> ComplianceCheck:
    return ComplianceCheck(check_id, CheckStatus.FAIL, message, _TOOL_REGISTRATION_PATH, line)


def _tool_definition_name(root: Path, module: str, factory: str) -> str | None:
    relative = "src/" + module.replace(".", "/") + ".py"
    source = _optional_text(root, relative)
    if source is None:
        return None
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError:
        return None

    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = value

    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == factory
        ),
        None,
    )
    if function is None:
        return None

    resolved: list[str] = []
    for node in ast.walk(function):
        if not isinstance(node, ast.Call) or _dotted_name(node.func) not in _TOOL_DEFINITION_NAMES:
            continue
        argument = _call_argument(node, "name")
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            resolved.append(argument.value)
        elif isinstance(argument, ast.Name) and argument.id in constants:
            resolved.append(constants[argument.id])
        else:
            return None
    return resolved[0] if len(resolved) == 1 else None


def _specification_tool_names(text: str) -> tuple[tuple[str, int], ...]:
    names: list[tuple[str, int]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _SPECIFICATION_TOOL_HEADING.fullmatch(line)
        if match is not None:
            names.append((match.group(1), line_number))
    return tuple(names)


def _readme_tool_names(text: str) -> tuple[tuple[str, int], ...] | None:
    names: list[tuple[str, int]] = []
    found = False
    in_section = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            if in_section:
                break
            in_section = line.strip() == _README_TOOL_SECTION
            found = found or in_section
            continue
        if in_section:
            match = _README_TOOL_ROW.fullmatch(line.strip())
            if match is not None:
                names.append((match.group(1), line_number))
    return tuple(names) if found else None


def _compare_documented_tools(
    check_id: str,
    registered: Sequence[str],
    documented: Sequence[tuple[str, int]],
    relative: str,
    label: str,
) -> list[ComplianceCheck]:
    failures: list[ComplianceCheck] = []
    seen: set[str] = set()
    for name, line in documented:
        if _IDENTIFIER.fullmatch(name) is None:
            message = f"{label} lists a tool entry that is not a plain identifier"
        elif name in seen:
            message = f"{label} documents the same tool more than once: {name}"
        elif name not in registered:
            seen.add(name)
            message = f"{label} documents a tool that is not registered: {name}"
        else:
            seen.add(name)
            continue
        failures.append(ComplianceCheck(check_id, CheckStatus.FAIL, message, relative, line))
    for name in registered:
        if name not in seen:
            failures.append(
                ComplianceCheck(
                    check_id,
                    CheckStatus.FAIL,
                    f"registered tool is not documented in {label}: {name}",
                    relative,
                )
            )
    return failures


def _check_adr_records(root: Path) -> tuple[ComplianceCheck, ...]:
    status_id = "documentation.adr_status"
    index_id = "documentation.adr_index"
    directory = _repository_directory(root, _ADR_DIRECTORY)
    entries: list[Path] | None = None
    if directory is not None:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            entries = None
    if entries is None:
        message = f"ADR directory is missing or not an ordinary directory: {_ADR_DIRECTORY}"
        return (
            ComplianceCheck(status_id, CheckStatus.FAIL, message, _ADR_DIRECTORY),
            ComplianceCheck(index_id, CheckStatus.FAIL, message, _ADR_DIRECTORY),
        )

    failures: list[ComplianceCheck] = []
    statuses: dict[str, tuple[str, str]] = {}
    numbers: set[str] = set()
    for entry in entries:
        name = entry.name
        if name in {"README.md", _ADR_TEMPLATE_NAME}:
            continue
        relative = f"{_ADR_DIRECTORY}/{name}"
        name_match = _ADR_FILE_NAME.fullmatch(name)
        if name_match is None:
            message = (
                "ADR file name must be NNNN- followed by lowercase words joined by hyphens"
                if _ADR_CANDIDATE_NAME.fullmatch(name) is not None
                else "ADR directory contains a file that is not an NNNN-title.md record"
            )
            failures.append(ComplianceCheck(status_id, CheckStatus.FAIL, message, relative))
            continue
        text = _optional_text(root, relative)
        if text is None:
            failures.append(_missing_document(status_id, relative))
            continue
        number = name_match.group(1)
        header_failures, status = _adr_header(status_id, relative, number, text)
        failures.extend(header_failures)
        if number in numbers:
            failures.append(
                ComplianceCheck(
                    status_id,
                    CheckStatus.FAIL,
                    f"ADR number is used by more than one file: {number}",
                    relative,
                )
            )
            continue
        numbers.add(number)
        if status is not None:
            statuses[number] = (name, status)

    if failures:
        status_checks = tuple(failures)
    else:
        status_checks = (
            ComplianceCheck(
                status_id,
                CheckStatus.PASS,
                f"{len(statuses)} ADRs have matching title numbers and allowed status values",
                _ADR_DIRECTORY,
            ),
        )
    return (*status_checks, *_check_adr_index(root, index_id, statuses))


def _adr_header(
    check_id: str, relative: str, number: str, text: str
) -> tuple[list[ComplianceCheck], str | None]:
    failures: list[ComplianceCheck] = []
    lines = text.splitlines()
    first = next(((index, line) for index, line in enumerate(lines, start=1) if line.strip()), None)
    title = None if first is None else _ADR_TITLE.fullmatch(first[1].strip())
    if title is None or title.group(1) != number:
        failures.append(
            ComplianceCheck(
                check_id,
                CheckStatus.FAIL,
                "ADR title must start with '# ADR-NNNN' and use the number from its file name",
                relative,
                None if first is None else first[0],
            )
        )

    for line_number, line in enumerate(lines[:_ADR_HEADER_SCAN_LINES], start=1):
        if not line.startswith(_ADR_STATUS_PREFIX):
            continue
        status = line[len(_ADR_STATUS_PREFIX) :].strip()
        if status in _ADR_STATUSES or _ADR_SUPERSEDED_STATUS.fullmatch(status) is not None:
            return failures, status
        failures.append(
            ComplianceCheck(
                check_id,
                CheckStatus.FAIL,
                "ADR status is not one of the allowed values",
                relative,
                line_number,
            )
        )
        return failures, None

    failures.append(
        ComplianceCheck(
            check_id,
            CheckStatus.FAIL,
            f"ADR has no '{_ADR_STATUS_PREFIX}' line in its header",
            relative,
        )
    )
    return failures, None


def _check_adr_index(
    root: Path, check_id: str, statuses: Mapping[str, tuple[str, str]]
) -> tuple[ComplianceCheck, ...]:
    text = _optional_text(root, _ADR_INDEX_PATH)
    if text is None:
        return (_missing_document(check_id, _ADR_INDEX_PATH),)

    failures: list[ComplianceCheck] = []
    indexed: set[str] = set()
    for header, rows in _markdown_tables(text):
        if not header or header[0] != _ADR_INDEX_HEADER:
            continue
        for line_number, cells in rows:
            link = _ADR_INDEX_LINK.fullmatch(cells[0]) if len(cells) >= 3 else None
            if link is None:
                message = (
                    "ADR index row must start with a [NNNN](file.md) link and include a status"
                )
            elif link.group(1) in indexed:
                message = f"ADR index lists the same record more than once: {link.group(1)}"
            else:
                number = link.group(1)
                indexed.add(number)
                recorded = statuses.get(number)
                if recorded is None or recorded[0] != link.group(2):
                    message = f"ADR index row does not link to a valid record file: {number}"
                elif cells[2] != recorded[1]:
                    message = f"ADR index status differs from the record's status: {number}"
                else:
                    continue
            failures.append(
                ComplianceCheck(check_id, CheckStatus.FAIL, message, _ADR_INDEX_PATH, line_number)
            )

    for number in sorted(set(statuses) - indexed):
        failures.append(
            ComplianceCheck(
                check_id,
                CheckStatus.FAIL,
                f"ADR is missing from the index: {number}",
                _ADR_INDEX_PATH,
            )
        )
    if failures:
        return tuple(failures)
    return (
        ComplianceCheck(
            check_id,
            CheckStatus.PASS,
            f"ADR index lists all {len(statuses)} ADRs with matching status values",
            _ADR_INDEX_PATH,
        ),
    )


def _check_roadmap_statuses(root: Path) -> tuple[ComplianceCheck, ...]:
    check_id = "documentation.roadmap_status"
    text = _optional_text(root, _ROADMAP_PATH)
    if text is None:
        return (_missing_document(check_id, _ROADMAP_PATH),)

    failures: list[ComplianceCheck] = []
    tables = 0
    rows_checked = 0
    for header, rows in _markdown_tables(text):
        if _ROADMAP_STATUS_HEADER not in header:
            continue
        tables += 1
        status_index = header.index(_ROADMAP_STATUS_HEADER)
        pr_index = header.index(_ROADMAP_PR_HEADER) if _ROADMAP_PR_HEADER in header else None
        for line_number, cells in rows:
            if len(cells) != len(header):
                message = "roadmap table row has a different number of cells than its header"
            elif cells[status_index] not in _ROADMAP_STATUSES and not cells[
                status_index
            ].startswith(_ROADMAP_BLOCKED_STATUS_PREFIX):
                message = "roadmap status is not one of the allowed values"
            elif (
                cells[status_index] == _ROADMAP_COMPLETED_STATUS
                and pr_index is not None
                and cells[pr_index] in _EMPTY_TABLE_CELLS
            ):
                message = "completed roadmap item has no pull request reference"
            else:
                rows_checked += 1
                continue
            failures.append(
                ComplianceCheck(check_id, CheckStatus.FAIL, message, _ROADMAP_PATH, line_number)
            )

    if tables == 0:
        failures.append(
            ComplianceCheck(
                check_id,
                CheckStatus.FAIL,
                "roadmap has no table with a status column",
                _ROADMAP_PATH,
            )
        )
    if failures:
        return tuple(failures)
    return (
        ComplianceCheck(
            check_id,
            CheckStatus.PASS,
            f"{rows_checked} roadmap status cells in {tables} tables use allowed values",
            _ROADMAP_PATH,
        ),
    )


def _markdown_tables(
    text: str,
) -> list[tuple[tuple[str, ...], list[tuple[int, tuple[str, ...]]]]]:
    """Return each pipe table as its header cells plus numbered data rows."""

    lines = text.splitlines()
    tables: list[tuple[tuple[str, ...], list[tuple[int, tuple[str, ...]]]]] = []
    index = 0
    while index < len(lines) - 1:
        line = lines[index].strip()
        if not (line.startswith("|") and _TABLE_SEPARATOR.fullmatch(lines[index + 1].strip())):
            index += 1
            continue
        header = _table_cells(line)
        rows: list[tuple[int, tuple[str, ...]]] = []
        index += 2
        while index < len(lines) and lines[index].strip().startswith("|"):
            rows.append((index + 1, _table_cells(lines[index])))
            index += 1
        tables.append((header, rows))
    return tables


def _table_cells(line: str) -> tuple[str, ...]:
    inner = line.strip().removeprefix("|").removesuffix("|")
    return tuple(cell.strip() for cell in inner.split("|"))


def _check_specification_layout(root: Path) -> tuple[ComplianceCheck, ...]:
    check_id = "documentation.spec_layout"
    text = _optional_text(root, _SPECIFICATION_PATH)
    if text is None:
        return (_missing_document(check_id, _SPECIFICATION_PATH),)
    entries = _layout_entries(text)
    if entries is None:
        return (
            ComplianceCheck(
                check_id,
                CheckStatus.FAIL,
                "specification section 5.1 has no text directory tree",
                _SPECIFICATION_PATH,
            ),
        )

    failures: list[ComplianceCheck] = []
    directories: list[str] = []
    checked = 0
    for line_number, line in entries:
        match = _LAYOUT_ENTRY.fullmatch(line)
        if match is None:
            if line.strip():
                failures.append(
                    _layout_failure(
                        check_id, "specification layout line cannot be parsed", line_number
                    )
                )
            continue
        depth = len(match.group(1)) // 4
        name = match.group(2)
        if _LAYOUT_NAME.fullmatch(name) is None or ".." in name.split("/"):
            message = "specification layout entry is not a plain relative path"
        elif depth > len(directories):
            message = "specification layout entry is nested under a file or skips a level"
        else:
            parents = directories[:depth]
            relative = "".join(parents) + name
            checked += 1
            if name.endswith("/"):
                directories = [*parents, name]
                present = _repository_directory(root, relative.rstrip("/")) is not None
            else:
                directories = parents
                present = _is_regular_repository_file(root, relative)
            if present:
                continue
            message = f"specification section 5.1 lists a path that does not exist: {relative}"
        failures.append(_layout_failure(check_id, message, line_number))

    if failures:
        return tuple(failures)
    return (
        ComplianceCheck(
            check_id,
            CheckStatus.PASS,
            f"all {checked} paths in the specification section 5.1 layout exist",
            _SPECIFICATION_PATH,
        ),
    )


def _layout_entries(text: str) -> list[tuple[int, str]] | None:
    """Return the numbered lines below the root of the first tree in section 5.1."""

    lines = text.splitlines()
    heading = next(
        (index for index, line in enumerate(lines) if line.startswith(_LAYOUT_HEADING_PREFIX)),
        None,
    )
    if heading is None:
        return None
    start: int | None = None
    for index in range(heading + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("## ") or stripped.startswith("### "):
            return None
        if stripped == "```text":
            start = index + 1
            break
    if start is None or start >= len(lines) or lines[start].strip() == "```":
        return None
    entries: list[tuple[int, str]] = []
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == "```":
            return entries
        entries.append((index + 1, lines[index]))
    return None


def _layout_failure(check_id: str, message: str, line: int) -> ComplianceCheck:
    return ComplianceCheck(check_id, CheckStatus.FAIL, message, _SPECIFICATION_PATH, line)


def _repository_directory(root: Path, relative: str) -> Path | None:
    try:
        path = _repository_path(root, relative)
        if path.is_symlink():
            return None
        metadata = path.stat(follow_symlinks=False)
        path.resolve(strict=True).relative_to(root)
    except (ComplianceInfrastructureError, OSError, ValueError):
        return None
    return path if stat.S_ISDIR(metadata.st_mode) else None


def _id_component(value: str) -> str:
    return _SAFE_CHECK_ID_COMPONENT.sub("_", value.casefold()).strip("_") or "unknown"


def _check_sort_key(check: ComplianceCheck) -> tuple[str, str, int, str, str]:
    return (
        check.check_id,
        "" if check.path is None else check.path,
        0 if check.line is None else check.line,
        check.status.value,
        check.message,
    )
