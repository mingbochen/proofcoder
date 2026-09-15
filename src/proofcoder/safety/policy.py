"""Project command policy: a frozen, explicitly authorized extension of default deny.

The file this module reads is repository content, and specification section 10.3 treats
repository content as untrusted. Nothing here decides that a policy may be used; the
caller does, by naming the file. This module only turns a named file into an immutable
object, and refuses anything that would relax rather than extend the built-in decision.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

from proofcoder.errors import ProofCoderError

POLICY_FILENAME = "proofcoder.toml"
POLICY_SCHEMA_VERSION = 1
MAX_POLICY_BYTES = 64 * 1024
MAX_POLICY_ENTRIES = 32
MAX_POLICY_TOKENS = 64
MAX_POLICY_TOKEN_CHARS = 64

# A policy declares the class of a command so that evidence never has to be guessed
# from a program name. These are the classes the verification tracker already knows;
# a policy may choose among them but may not invent one.
POLICY_COMMAND_KINDS = frozenset({"build", "script", "static_check", "test"})

_FORBIDDEN_EXECUTABLE_SUFFIXES = frozenset({".bat", ".cmd", ".ps1", ".sh"})
_TOP_LEVEL_FIELDS = frozenset({"command", "schema_version"})
_ENTRY_FIELDS = frozenset({"decision", "executable", "kind", "options", "subcommands"})


class CommandDecision(StrEnum):
    """What the single decision entry point concluded about one command.

    ``CONFIRM`` is not a softer ``DENY``: it names a command whose risk a person can
    judge, which is why the categories in specification section 10.4.5 never reach it.
    """

    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


class CommandPolicyFileError(ProofCoderError):
    """A stable, non-sensitive project policy failure safe to show the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PolicyEntry:
    """One declared executable together with everything it may be invoked with."""

    executable: str
    subcommands: tuple[str, ...]
    options: tuple[str, ...]
    decision: CommandDecision
    kind: str


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """An immutable snapshot of one policy file, taken before the run starts."""

    entries: tuple[PolicyEntry, ...]
    source: str
    digest: str

    def entry_for(self, executable_name: str) -> PolicyEntry | None:
        """Return the declaration for one canonical executable name, if any."""

        folded = executable_name.casefold()
        for entry in self.entries:
            if entry.executable == folded:
                return entry
        return None


def load_command_policy(
    path: Path,
    *,
    workspace: Path,
    reserved_executables: Iterable[str],
) -> CommandPolicy:
    """Read, validate, and freeze one policy file the user explicitly named.

    Validation is all-or-nothing on purpose: a policy that is partly wrong must not
    partly apply, because the half that loaded would be an allowance nobody reviewed.
    """

    workspace_root = workspace.resolve(strict=True)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise CommandPolicyFileError(
            "POLICY_NOT_FOUND", "the named command policy file does not exist"
        ) from None
    if not resolved.is_file():
        raise CommandPolicyFileError(
            "POLICY_NOT_A_FILE", "the named command policy path is not a regular file"
        )

    try:
        raw = resolved.read_bytes()
    except OSError:
        raise CommandPolicyFileError(
            "POLICY_UNREADABLE", "the command policy file could not be read"
        ) from None
    if len(raw) > MAX_POLICY_BYTES:
        raise CommandPolicyFileError(
            "POLICY_TOO_LARGE",
            f"the command policy file exceeds {MAX_POLICY_BYTES} bytes",
        )

    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise CommandPolicyFileError(
            "POLICY_INVALID", "the command policy file must be valid UTF-8 TOML"
        ) from None

    entries = _parse_document(document, reserved=frozenset(reserved_executables))
    return CommandPolicy(
        entries=entries,
        source=_display_source(resolved, workspace_root),
        digest=hashlib.sha256(raw).hexdigest(),
    )


def is_command_policy_path(relative_path: str) -> bool:
    """Return whether one workspace-relative path names a command policy file.

    Matching is by filename anywhere in the tree, the way credential files are matched.
    The model has no business authoring a command policy at any depth, and a rule tied
    to the file actually loaded would leave every other candidate writable.
    """

    name = PurePosixPath(relative_path).name.casefold()
    return name == POLICY_FILENAME


def workspace_policy_path(workspace: Path) -> Path:
    """Return the conventional policy path for one workspace."""

    return workspace / POLICY_FILENAME


def unloaded_policy_warning(source: str) -> dict[str, object]:
    """Describe a policy file that was found but deliberately not applied."""

    return {
        "code": "COMMAND_POLICY_NOT_LOADED",
        "message": (
            f"COMMAND_POLICY_NOT_LOADED: a project command policy exists at {source} "
            "but was not loaded; pass --command-policy to authorize it"
        ),
        "policy_source": source,
    }


def _display_source(resolved: Path, workspace_root: Path) -> str:
    try:
        return resolved.relative_to(workspace_root).as_posix()
    except ValueError:
        # A policy kept outside the workspace is legitimate -- it is the one place the
        # model can never reach -- so report its name rather than a path that would
        # leak the operator's directory layout into the trace.
        return resolved.name


def _parse_document(document: object, *, reserved: frozenset[str]) -> tuple[PolicyEntry, ...]:
    if not isinstance(document, dict):
        raise CommandPolicyFileError("POLICY_INVALID", "the command policy must be a TOML table")
    unknown = set(document) - _TOP_LEVEL_FIELDS
    if unknown:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            f"unknown top-level policy field: {sorted(unknown)[0]}",
        )
    version = document.get("schema_version")
    if type(version) is not int or version != POLICY_SCHEMA_VERSION:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            f"command policy schema_version must be {POLICY_SCHEMA_VERSION}",
        )

    declared = document.get("command", [])
    if not isinstance(declared, list) or not declared:
        raise CommandPolicyFileError(
            "POLICY_INVALID", "the command policy must declare at least one [[command]]"
        )
    if len(declared) > MAX_POLICY_ENTRIES:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            f"the command policy declares more than {MAX_POLICY_ENTRIES} commands",
        )

    entries: list[PolicyEntry] = []
    seen: set[str] = set()
    for item in declared:
        entry = _parse_entry(item, reserved=reserved)
        if entry.executable in seen:
            raise CommandPolicyFileError(
                "POLICY_DUPLICATE_EXECUTABLE",
                f"the command policy declares '{entry.executable}' more than once",
            )
        seen.add(entry.executable)
        entries.append(entry)
    return tuple(entries)


def _parse_entry(value: object, *, reserved: frozenset[str]) -> PolicyEntry:
    if not isinstance(value, Mapping):
        raise CommandPolicyFileError("POLICY_INVALID", "each [[command]] must be a table")
    unknown = set(value) - _ENTRY_FIELDS
    if unknown:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            f"unknown policy command field: {sorted(unknown)[0]}",
        )

    executable = _executable_name(value.get("executable"))
    if executable in reserved:
        # Shadowing a built-in name is the one way a policy could relax rather than
        # extend: it would replace the per-subcommand analysis the built-in performs,
        # or reopen a name the built-in refuses outright.
        raise CommandPolicyFileError(
            "POLICY_SHADOWS_BUILTIN",
            f"'{executable}' is decided by the built-in policy and cannot be redeclared",
        )

    decision_value = value.get("decision")
    if decision_value not in {CommandDecision.ALLOW.value, CommandDecision.CONFIRM.value}:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            "each policy command decision must be 'allow' or 'confirm'",
        )
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in POLICY_COMMAND_KINDS:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            f"each policy command kind must be one of {sorted(POLICY_COMMAND_KINDS)}",
        )

    return PolicyEntry(
        executable=executable,
        subcommands=_token_list(value.get("subcommands", []), "subcommands", option=False),
        options=_token_list(value.get("options", []), "options", option=True),
        decision=CommandDecision(decision_value),
        kind=kind,
    )


def _executable_name(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CommandPolicyFileError(
            "POLICY_INVALID", "each policy command executable must be a bare program name"
        )
    if len(value) > MAX_POLICY_TOKEN_CHARS:
        raise CommandPolicyFileError("POLICY_INVALID", "policy executable name is too long")
    if "/" in value or "\\" in value or value.startswith("-") or "\0" in value:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            "policy executables are bare program names resolved on the sanitized PATH",
        )
    folded = value.casefold()
    if Path(folded).suffix in _FORBIDDEN_EXECUTABLE_SUFFIXES:
        raise CommandPolicyFileError(
            "POLICY_INVALID",
            "shell, batch, and PowerShell script executables cannot be declared",
        )
    return folded


def _token_list(value: object, field: str, *, option: bool) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CommandPolicyFileError("POLICY_INVALID", f"policy {field} must be a string list")
    if len(value) > MAX_POLICY_TOKENS:
        raise CommandPolicyFileError("POLICY_INVALID", f"policy {field} declares too many entries")
    tokens: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or "\0" in item
            or len(item) > MAX_POLICY_TOKEN_CHARS
        ):
            raise CommandPolicyFileError(
                "POLICY_INVALID", f"policy {field} contains an invalid token"
            )
        if option and not item.startswith("-"):
            raise CommandPolicyFileError("POLICY_INVALID", "policy options must start with '-'")
        if not option and item.startswith("-"):
            raise CommandPolicyFileError(
                "POLICY_INVALID", "policy subcommands must not start with '-'"
            )
        tokens.append(item)
    return tuple(_deduplicate(tokens))


def _deduplicate(tokens: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            ordered.append(token)
    return ordered
