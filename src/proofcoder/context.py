"""Locally maintained Chat Completions message history."""

from __future__ import annotations

import json
from collections import Counter

from proofcoder.errors import MessageHistoryError
from proofcoder.protocol import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)


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
            self._messages[-1], (SystemMessage, AssistantMessage)
        ):
            raise MessageHistoryError("A user message must follow system or assistant.")
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
        serialized: list[dict[str, object]] = []
        for message in self._messages:
            if isinstance(message, SystemMessage | UserMessage):
                serialized.append({"role": message.role, "content": message.content})
            elif isinstance(message, AssistantMessage):
                payload: dict[str, object] = {
                    "role": message.role,
                    "content": message.content,
                }
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
                serialized.append(payload)
            else:
                serialized.append(
                    {
                        "role": message.role,
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
        return serialized

    def _require_no_pending_results(self) -> None:
        if self._pending_tool_results:
            raise MessageHistoryError("All assistant tool calls require matching tool results.")
