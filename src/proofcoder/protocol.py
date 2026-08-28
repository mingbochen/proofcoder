"""Provider-independent messages and run results owned by ProofCoder."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeAlias

if TYPE_CHECKING:
    from proofcoder.context import MessageHistory


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts reported by a Chat Completions response."""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class FunctionCall:
    """A model-requested function name and its untouched JSON arguments."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider-independent function tool call."""

    id: str
    function: FunctionCall
    type: str = "function"


@dataclass(frozen=True, slots=True)
class SystemMessage:
    """A system instruction in local message history."""

    content: str
    role: Literal["system"] = field(default="system", init=False)


@dataclass(frozen=True, slots=True)
class UserMessage:
    """A user message in local message history."""

    content: str
    role: Literal["user"] = field(default="user", init=False)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """A complete normalized assistant response, including private reasoning."""

    content: str | None
    reasoning_content: str | None
    finish_reason: str | None
    usage: TokenUsage | None
    tool_calls: tuple[ToolCall, ...] = ()
    role: Literal["assistant"] = field(default="assistant", init=False)


ModelResponse: TypeAlias = AssistantMessage


@dataclass(frozen=True, slots=True)
class ToolMessage:
    """A JSON tool result paired to one assistant tool call."""

    tool_call_id: str
    content: str
    role: Literal["tool"] = field(default="tool", init=False)


Message: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolMessage


class TerminationReason(StrEnum):
    """Controlled termination reasons implemented through Stage D2."""

    FINISH_TASK = "finish_task"
    MODEL_STOPPED = "model_stopped"
    MAX_STEPS = "max_steps"
    MAX_TIME = "max_time"
    MAX_CONSECUTIVE_FAILURES = "max_consecutive_failures"
    NO_PROGRESS = "no_progress"
    INTERRUPTED = "interrupted"
    API_ERROR = "api_error"
    CONFIGURATION_ERROR = "configuration_error"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    INTERNAL_ERROR = "internal_error"


class CompletionStatus(StrEnum):
    """Locally determined outcomes for an accepted ``finish_task`` call."""

    COMPLETED_VERIFIED = "completed_verified"
    COMPLETED_UNVERIFIED = "completed_unverified"
    COMPLETED_NO_CHANGES = "completed_no_changes"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Observable result of one bounded agent run."""

    termination_reason: TerminationReason
    final_text: str | None
    history: MessageHistory
    model_call_count: int
    tool_call_count: int
    tool_error_count: int
    completion_status: CompletionStatus | None = None
    final_report: str | None = None
    changed_files: tuple[str, ...] = ()
    verification_command: tuple[str, ...] | None = None
    verification_cwd: str | None = None
    verification_exit_code: int | None = None
    elapsed_seconds: float = 0.0
    api_attempt_count: int = 0
    api_retry_count: int = 0
    context_compaction_count: int = 0
    consecutive_failure_count: int = 0
    no_progress_count: int = 0
    warnings: tuple[str, ...] = ()
    input_token_count: int = 0
    output_token_count: int = 0
    run_id: str = ""
    trace_path: str | None = None
    trace_complete: bool = True
    event_count: int = 0
