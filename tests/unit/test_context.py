"""Message ordering, pairing, and serialization tests."""

from __future__ import annotations

import pytest

from proofcoder.context import MessageHistory
from proofcoder.errors import MessageHistoryError
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall

REASONING_SENTINEL = "private-reasoning"


def _assistant(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=REASONING_SENTINEL,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=tuple(calls),
    )


def _call(call_id: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name="list_files", arguments=arguments),
    )


def test_normal_message_order_serializes_to_plain_payloads() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant(_call("call-1")))
    history.add_tool("call-1", '{"ok":true}')
    history.add_assistant(_assistant(content="done"))

    payloads = history.to_api_messages()

    assert [payload["role"] for payload in payloads] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert payloads[3] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"ok":true}',
    }


def test_reasoning_and_multiple_tool_calls_survive_serialization() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(
        _assistant(
            _call("first", '{"path":"."}'),
            _call("second", '{"path":"src"}'),
        )
    )
    history.add_tool("first", '{"ok":true}')
    history.add_tool("second", '{"ok":true}')

    assistant = history.to_api_messages()[2]

    assert assistant["reasoning_content"] == REASONING_SENTINEL
    assert assistant["tool_calls"] == [
        {
            "id": "first",
            "type": "function",
            "function": {"name": "list_files", "arguments": '{"path":"."}'},
        },
        {
            "id": "second",
            "type": "function",
            "function": {"name": "list_files", "arguments": '{"path":"src"}'},
        },
    ]


def test_serialization_rejects_missing_tool_result() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant(_call("pending")))

    with pytest.raises(MessageHistoryError, match="matching tool results"):
        history.to_api_messages()


def test_wrong_tool_call_id_is_rejected() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant(_call("expected")))

    with pytest.raises(MessageHistoryError, match="missing, incorrect"):
        history.add_tool("wrong", "{}")


@pytest.mark.parametrize("content", ["not-json", "[]"])
def test_tool_result_must_be_a_json_object(content: str) -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant(_call("expected")))

    with pytest.raises(MessageHistoryError, match="JSON"):
        history.add_tool("expected", content)


def test_duplicate_tool_result_is_rejected() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant(_call("once")))
    history.add_tool("once", "{}")

    with pytest.raises(MessageHistoryError, match="already satisfied"):
        history.add_tool("once", "{}")


def test_illegal_message_order_is_rejected() -> None:
    history = MessageHistory()

    with pytest.raises(MessageHistoryError, match="user message"):
        history.add_user("too early")

    history.add_system("system")
    with pytest.raises(MessageHistoryError, match="first and unique"):
        history.add_system("duplicate")
    with pytest.raises(MessageHistoryError, match="assistant message"):
        history.add_assistant(_assistant())


def test_duplicate_call_ids_are_counted_for_pairing() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant(_call("same"), _call("same")))
    history.add_tool("same", '{"ok":false}')
    history.add_tool("same", '{"ok":false}')

    assert len(history.to_api_messages()) == 5
