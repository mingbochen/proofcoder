"""Validated ``finish_task`` request and local evidence-gated completion."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from proofcoder.protocol import CompletionStatus
from proofcoder.safety.paths import WorkspacePathError, resolve_workspace_argument_path
from proofcoder.state import CommandObservation
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.verification import VerificationTracker

FINISH_TASK_NAME = "finish_task"
MAX_SUMMARY_CHARACTERS = 4000
MAX_CHANGED_FILES = 256
MAX_PATH_CHARACTERS = 1024
MAX_VERIFICATION_ARGUMENTS = 64
MAX_VERIFICATION_ARGUMENT_CHARACTERS = 4096
MAX_LIMITATIONS = 64
MAX_LIMITATION_CHARACTERS = 2000
MAX_BLOCKED_REASON_CHARACTERS = 4000


@dataclass(frozen=True, slots=True)
class FinishTaskRequest:
    """Normalized model-provided explanatory fields for ``finish_task``."""

    summary: str
    changed_files: tuple[str, ...]
    verification_command: tuple[str, ...] | None
    limitations: tuple[str, ...]
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class FinishOutcome:
    """One local completion decision and its model-visible result."""

    status: CompletionStatus
    result: ToolResult
    final_report: str


def create_finish_task_tool(workspace: Path) -> ToolDefinition:
    """Create a non-mutating finish request tool bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def preflight(arguments: Mapping[str, object]) -> ToolResult | None:
        parsed = parse_finish_task_request(workspace_root, arguments)
        return parsed if isinstance(parsed, ToolResult) else None

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        parsed = parse_finish_task_request(workspace_root, arguments)
        if isinstance(parsed, ToolResult):
            return parsed
        return ToolResult.success(
            {
                "accepted": True,
                "message": "AgentLoop must determine completion from injected local evidence",
            }
        )

    return ToolDefinition(
        name=FINISH_TASK_NAME,
        description=(
            "Request completion or report a blocker. This must be the only tool call in the "
            "assistant response. The tool never runs verification_command or changes files; "
            "completion status, changed files, and verification are determined only from local "
            "tool evidence recorded by AgentLoop."
        ),
        parameters={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Short non-empty description of the outcome.",
                    "minLength": 1,
                    "maxLength": MAX_SUMMARY_CHARACTERS,
                },
                "changed_files": {
                    "type": "array",
                    "description": "Explanatory workspace-relative changed-file claim.",
                    "maxItems": MAX_CHANGED_FILES,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PATH_CHARACTERS,
                    },
                    "default": [],
                },
                "verification_command": {
                    "type": ["array", "null"],
                    "description": (
                        "Optional explanatory argv claim; finish_task never executes this value."
                    ),
                    "minItems": 1,
                    "maxItems": MAX_VERIFICATION_ARGUMENTS,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_VERIFICATION_ARGUMENT_CHARACTERS,
                    },
                    "default": None,
                },
                "limitations": {
                    "type": "array",
                    "description": "Known limitations as bounded non-empty strings.",
                    "maxItems": MAX_LIMITATIONS,
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_LIMITATION_CHARACTERS,
                    },
                    "default": [],
                },
                "blocked_reason": {
                    "type": ["string", "null"],
                    "description": "Non-empty reason when the task cannot be completed safely.",
                    "minLength": 1,
                    "maxLength": MAX_BLOCKED_REASON_CHARACTERS,
                    "default": None,
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
        execute=execute,
        preflight=preflight,
    )


def parse_finish_task_request(
    workspace: Path,
    arguments: Mapping[str, object],
) -> FinishTaskRequest | ToolResult:
    """Apply semantic and nested-array validation shared by preflight and execution."""

    summary = arguments.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return _invalid_arguments("summary must be a non-empty string")

    changed_value = arguments.get("changed_files", [])
    changed_files = _validate_string_array(
        changed_value,
        field_name="changed_files",
        maximum_items=MAX_CHANGED_FILES,
        maximum_characters=MAX_PATH_CHARACTERS,
        allow_empty=True,
    )
    if isinstance(changed_files, ToolResult):
        return changed_files

    normalized_files: list[str] = []
    for claimed_path in changed_files:
        try:
            _, relative_path = resolve_workspace_argument_path(
                workspace,
                workspace,
                claimed_path,
            )
        except WorkspacePathError as error:
            return ToolResult.failure(error.code, str(error), retryable=True)
        except (OSError, ValueError):
            return _invalid_arguments("changed_files contains an invalid path")
        if relative_path == ".":
            return _invalid_arguments("changed_files entries must identify files")
        if relative_path not in normalized_files:
            normalized_files.append(relative_path)

    verification_value = arguments.get("verification_command")
    verification_command: tuple[str, ...] | None
    if verification_value is None:
        verification_command = None
    else:
        parsed_verification = _validate_string_array(
            verification_value,
            field_name="verification_command",
            maximum_items=MAX_VERIFICATION_ARGUMENTS,
            maximum_characters=MAX_VERIFICATION_ARGUMENT_CHARACTERS,
            allow_empty=False,
        )
        if isinstance(parsed_verification, ToolResult):
            return parsed_verification
        verification_command = parsed_verification

    limitations_value = arguments.get("limitations", [])
    limitations = _validate_string_array(
        limitations_value,
        field_name="limitations",
        maximum_items=MAX_LIMITATIONS,
        maximum_characters=MAX_LIMITATION_CHARACTERS,
        allow_empty=True,
    )
    if isinstance(limitations, ToolResult):
        return limitations

    blocked_value = arguments.get("blocked_reason")
    if blocked_value is not None and (
        not isinstance(blocked_value, str) or not blocked_value.strip()
    ):
        return _invalid_arguments("blocked_reason must be a non-empty string or null")

    return FinishTaskRequest(
        summary=summary,
        changed_files=tuple(normalized_files),
        verification_command=verification_command,
        limitations=limitations,
        blocked_reason=blocked_value,
    )


def build_finish_outcome(
    request: FinishTaskRequest,
    tracker: VerificationTracker,
) -> FinishOutcome:
    """Determine completion solely from the injected tracker and model blocker flag."""

    state = tracker.state
    verification = tracker.valid_verification
    if request.blocked_reason is not None:
        status = CompletionStatus.BLOCKED
    elif not state.changed_files:
        status = CompletionStatus.COMPLETED_NO_CHANGES
    elif verification is not None:
        status = CompletionStatus.COMPLETED_VERIFIED
    else:
        status = CompletionStatus.COMPLETED_UNVERIFIED

    warnings = _claim_warnings(request, state.changed_files, verification)
    command_history = [_command_to_dict(item) for item in state.command_observations]
    verification_data = None if verification is None else _command_to_dict(verification)
    result = ToolResult.success(
        {
            "completion_status": status.value,
            "summary": request.summary,
            "changed_files": list(state.changed_files),
            "verification": verification_data,
            "command_observations": command_history,
            "limitations": list(request.limitations),
            "blocked_reason": request.blocked_reason,
            "warnings": list(warnings),
            "verification_command_executed_by_finish_task": False,
        },
        warnings=warnings,
    )
    return FinishOutcome(
        status=status,
        result=result,
        final_report=_build_final_report(
            request=request,
            status=status,
            changed_files=state.changed_files,
            command_observations=tuple(state.command_observations),
            verification=verification,
            warnings=warnings,
        ),
    )


def _validate_string_array(
    value: object,
    *,
    field_name: str,
    maximum_items: int,
    maximum_characters: int,
    allow_empty: bool,
) -> tuple[str, ...] | ToolResult:
    if not isinstance(value, list):
        return _invalid_arguments(f"{field_name} must be a string array")
    if not allow_empty and not value:
        return _invalid_arguments(f"{field_name} must not be empty when provided")
    if len(value) > maximum_items:
        return _invalid_arguments(f"{field_name} contains too many items")
    parsed: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > maximum_characters
            or "\0" in item
        ):
            return _invalid_arguments(f"{field_name} contains an invalid string")
        parsed.append(item)
    return tuple(parsed)


def _claim_warnings(
    request: FinishTaskRequest,
    actual_changed_files: tuple[str, ...],
    verification: CommandObservation | None,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if set(request.changed_files) != set(actual_changed_files):
        warnings.append(
            "MODEL_CHANGED_FILES_MISMATCH: model changed_files differs from local evidence; "
            "local evidence was retained"
        )
    if request.verification_command is not None:
        if verification is None:
            warnings.append(
                "MODEL_VERIFICATION_UNCONFIRMED: verification_command has no current valid "
                "local execution evidence"
            )
        elif request.verification_command != verification.argv:
            warnings.append(
                "MODEL_VERIFICATION_MISMATCH: verification_command differs from current valid "
                "local execution evidence"
            )
    return tuple(warnings)


def _command_to_dict(observation: CommandObservation) -> dict[str, object]:
    return {
        "argv": list(observation.argv),
        "cwd": observation.cwd,
        "exit_code": observation.exit_code,
        "timed_out": observation.timed_out,
        "command_kind": observation.command_kind,
        "event_sequence": observation.event_sequence,
        "accepted_as_verification": observation.accepted_as_verification,
    }


def _build_final_report(
    *,
    request: FinishTaskRequest,
    status: CompletionStatus,
    changed_files: tuple[str, ...],
    command_observations: tuple[CommandObservation, ...],
    verification: CommandObservation | None,
    warnings: tuple[str, ...],
) -> str:
    lines = [f"Completion status: {status.value}", f"Summary: {request.summary}"]
    lines.append("Changed files (local evidence):")
    lines.extend(f"- {path}" for path in changed_files)
    if not changed_files:
        lines.append("- none")

    lines.append("Command observations (local evidence):")
    for observation in command_observations:
        argv = json.dumps(list(observation.argv), ensure_ascii=False, separators=(",", ":"))
        lines.append(
            f"- event={observation.event_sequence} cwd={observation.cwd} argv={argv} "
            f"exit_code={observation.exit_code} timed_out={str(observation.timed_out).lower()} "
            f"kind={observation.command_kind} "
            f"verification={str(observation.accepted_as_verification).lower()}"
        )
    if not command_observations:
        lines.append("- none")

    if verification is None:
        lines.append("Valid verification after latest modification: none")
    else:
        lines.append(
            "Valid verification after latest modification: "
            f"event={verification.event_sequence} exit_code={verification.exit_code}"
        )

    lines.append("Limitations:")
    lines.extend(f"- {limitation}" for limitation in request.limitations)
    if not request.limitations:
        lines.append("- none")
    if request.blocked_reason is not None:
        lines.append(f"Blocked reason: {request.blocked_reason}")
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def _invalid_arguments(message: str) -> ToolResult:
    return ToolResult.failure("INVALID_ARGUMENTS", message, retryable=True)
