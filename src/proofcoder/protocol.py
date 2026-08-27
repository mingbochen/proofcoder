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
    """Stage B controlled termination reasons."""

    MODEL_STOPPED = "model_stopped"
    MAX_STEPS = "max_steps"
    API_ERROR = "api_error"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Observable result of one bounded Stage B agent run."""

    termination_reason: TerminationReason
    final_text: str | None
    history: MessageHistory
    model_call_count: int
    tool_call_count: int
    tool_error_count: int
