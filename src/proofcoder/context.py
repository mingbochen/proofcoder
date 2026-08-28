"""Full local history and deterministic budgeted API context views."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from proofcoder.errors import ContextBudgetError, MessageHistoryError
from proofcoder.llm.base import ChatMessagePayload, ToolSchema
from proofcoder.protocol import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)

if TYPE_CHECKING:
    from proofcoder.state import RunState

DEFAULT_CONTEXT_BUDGET_BYTES = 256 * 1024
CONTEXT_TARGET_RATIO = 0.9
MIN_RECENT_ATOMIC_GROUPS = 1
STATE_SUMMARY_PREFIX = "ProofCoder program-generated local run facts: "


class MessageHistory:
    """Maintain legal message order and tool-call/result pairing."""

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self._pending_tool_results: Counter[str] = Counter()

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable view of all complete local messages."""

        return tuple(self._messages)

    def add_system(self, content: str) -> None:
        """Add the single required first system message."""

        if self._messages:
            raise MessageHistoryError("The system message must be first and unique.")
        self._messages.append(SystemMessage(content=content))

    def add_user(self, content: str) -> None:
        """Add a user message when no tool results are outstanding."""

        if not self._messages or not isinstance(
            self._messages[-1], (SystemMessage, AssistantMessage, ToolMessage)
        ):
            raise MessageHistoryError("A user message must follow a complete message group.")
        self._require_no_pending_results()
        self._messages.append(UserMessage(content=content))

    def add_assistant(self, message: AssistantMessage) -> None:
        """Add an assistant response and begin any tool-result group."""

        if not self._messages or not isinstance(self._messages[-1], (UserMessage, ToolMessage)):
            raise MessageHistoryError("An assistant message must follow user or tool results.")
        self._require_no_pending_results()
        self._messages.append(message)
        self._pending_tool_results.update(call.id for call in message.tool_calls)

    def add_tool(self, tool_call_id: str, content: str) -> None:
        """Add exactly one JSON result for one pending tool-call occurrence."""

        if self._pending_tool_results[tool_call_id] <= 0:
            raise MessageHistoryError("Tool result ID is missing, incorrect, or already satisfied.")
        if not isinstance(self._messages[-1], (AssistantMessage, ToolMessage)):
            raise MessageHistoryError("A tool result must follow its assistant tool-call group.")
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            raise MessageHistoryError("Tool result content must be valid JSON.") from None
        if not isinstance(decoded, dict):
            raise MessageHistoryError("Tool result content must be a JSON object.")
        self._messages.append(ToolMessage(tool_call_id=tool_call_id, content=content))
        self._pending_tool_results[tool_call_id] -= 1
        if self._pending_tool_results[tool_call_id] == 0:
            del self._pending_tool_results[tool_call_id]

    def to_api_messages(self) -> list[dict[str, object]]:
        """Serialize complete history into plain Chat Completions messages."""

        self._require_no_pending_results()
        return [serialize_message(message) for message in self._messages]

    def _require_no_pending_results(self) -> None:
        if self._pending_tool_results:
            raise MessageHistoryError("All assistant tool calls require matching tool results.")


@dataclass(frozen=True, slots=True)
class ContextView:
    """One immutable request view derived without altering full local history."""

    messages: tuple[dict[str, object], ...]
    byte_count: int
    compressed_group_count: int


class ContextManager:
    """Build bounded API views while preserving complete assistant/tool groups."""

    def __init__(
        self,
        *,
        budget_bytes: int = DEFAULT_CONTEXT_BUDGET_BYTES,
        target_ratio: float = CONTEXT_TARGET_RATIO,
        min_recent_groups: int = MIN_RECENT_ATOMIC_GROUPS,
    ) -> None:
        if budget_bytes < 1:
            raise ValueError("context budget must be positive")
        if not 0 < target_ratio <= 1:
            raise ValueError("context target ratio must be in (0, 1]")
        if min_recent_groups < 0:
            raise ValueError("minimum recent groups cannot be negative")
        self._budget_bytes = budget_bytes
        self._target_bytes = max(1, int(budget_bytes * target_ratio))
        self._min_recent_groups = min_recent_groups

    def build(
        self,
        history: MessageHistory,
        tools: Sequence[ToolSchema],
        state: RunState,
    ) -> ContextView:
        """Return a deterministic view or fail when required context cannot fit."""

        complete = history.to_api_messages()
        if len(complete) < 2:
            raise MessageHistoryError("Context requires a system message and original task.")
        groups = _interaction_groups(history.messages[2:])
        removed = 0

        messages = self._assemble(complete[:2], groups, state, removed)
        size = deterministic_request_bytes(messages, tools)
        if size <= self._budget_bytes:
            return ContextView(tuple(messages), size, 0)

        while len(groups) - removed > self._min_recent_groups:
            removed += 1
            messages = self._assemble(complete[:2], groups[removed:], state, removed)
            size = deterministic_request_bytes(messages, tools)
            if size <= self._target_bytes:
                return ContextView(tuple(messages), size, removed)

        messages = self._assemble(complete[:2], groups[removed:], state, removed)
        size = deterministic_request_bytes(messages, tools)
        if size > self._budget_bytes:
            raise ContextBudgetError(
                "Required messages and newest atomic interaction group exceed context budget."
            )
        return ContextView(tuple(messages), size, removed)

    @staticmethod
    def _assemble(
        fixed: Sequence[dict[str, object]],
        groups: Sequence[tuple[Message, ...]],
        state: RunState,
        removed: int,
    ) -> list[dict[str, object]]:
        system = dict(fixed[0])
        system["content"] = f"{system['content']}\n\n{_state_summary(state, removed)}"
        messages = [system, dict(fixed[1])]
        for group in groups:
            messages.extend(serialize_message(message) for message in group)
        return messages


def serialize_message(message: Message) -> dict[str, object]:
    """Serialize one locally owned message without changing provider payload fields."""

    if isinstance(message, SystemMessage | UserMessage):
        return {"role": message.role, "content": message.content}
    if isinstance(message, AssistantMessage):
        payload: dict[str, object] = {"role": message.role, "content": message.content}
        if message.reasoning_content is not None:
            payload["reasoning_content"] = message.reasoning_content
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        return payload
    return {
        "role": message.role,
        "tool_call_id": message.tool_call_id,
        "content": message.content,
    }


def deterministic_request_bytes(
    messages: Sequence[ChatMessagePayload],
    tools: Sequence[ToolSchema],
) -> int:
    """Measure canonical JSON UTF-8 bytes for messages and tool schemas."""

    payload = {"messages": list(messages), "tools": list(tools)}
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _interaction_groups(messages: Sequence[Message]) -> tuple[tuple[Message, ...], ...]:
    groups: list[tuple[Message, ...]] = []
    current: list[Message] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            if current:
                groups.append(tuple(current))
            current = [message]
        elif current:
            current.append(message)
        else:
            groups.append((message,))
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _state_summary(state: RunState, compressed_groups: int) -> str:
    verification = state.latest_verification
    facts: dict[str, object] = {
        "api_attempts": state.api_attempt_count,
        "api_retries": state.api_retry_count,
        "changed_files": list(state.changed_files),
        "compressed_groups": compressed_groups,
        "consecutive_failures": state.consecutive_failure_count,
        "last_modification_event": state.last_modification_event,
        "no_progress_repeats": state.no_progress_count,
        "stable_error_codes": list(state.stable_error_codes),
        "verification": None,
    }
    if verification is not None:
        facts["verification"] = {
            "argv": list(verification.argv),
            "cwd": verification.cwd,
            "event": verification.event_sequence,
            "exit_code": verification.exit_code,
        }
    return STATE_SUMMARY_PREFIX + json.dumps(
        facts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
