"""Budgeted API context tests with complete local history retained."""

from __future__ import annotations

import json

import pytest

from proofcoder.context import STATE_SUMMARY_PREFIX, ContextManager, MessageHistory
from proofcoder.errors import ContextBudgetError
from proofcoder.protocol import AssistantMessage, FunctionCall, ToolCall
from proofcoder.state import RunState


def _assistant(call_id: str, *, reasoning: str, arguments: str = "{}") -> AssistantMessage:
    return AssistantMessage(
        content=None,
        reasoning_content=reasoning,
        finish_reason="tool_calls",
        usage=None,
        tool_calls=(
            ToolCall(
                id=call_id,
                function=FunctionCall(name="probe", arguments=arguments),
            ),
        ),
    )


def _history() -> MessageHistory:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("original task")
    history.add_assistant(_assistant("old", reasoning="old reasoning"))
    history.add_tool("old", json.dumps({"ok": True, "data": {"output": "x" * 5000}}))
    history.add_assistant(_assistant("new", reasoning="new reasoning", arguments='{ "path": "." }'))
    history.add_tool("new", json.dumps({"ok": True, "data": {"value": 2}}))
    return history


def test_compression_removes_oldest_whole_group_and_preserves_newest_exactly() -> None:
    history = _history()
    state = RunState(original_task="original task")
    full = ContextManager(budget_bytes=100_000, target_ratio=1.0).build(history, (), state)

    view = ContextManager(
        budget_bytes=full.byte_count - 1,
        target_ratio=1.0,
    ).build(history, (), state)

    assert view.compressed_group_count == 1
    assert len(history.messages) == 6
    assert view.messages[1] == {"role": "user", "content": "original task"}
    assert STATE_SUMMARY_PREFIX in str(view.messages[0]["content"])
    assert all(message.get("tool_call_id") != "old" for message in view.messages)
    assistant = next(message for message in view.messages if message["role"] == "assistant")
    tool = next(message for message in view.messages if message["role"] == "tool")
    assert assistant["reasoning_content"] == "new reasoning"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{ "path": "." }'
    assert tool["tool_call_id"] == "new"


def test_required_fixed_messages_and_newest_group_cannot_be_partially_removed() -> None:
    history = _history()

    with pytest.raises(ContextBudgetError):
        ContextManager(budget_bytes=128).build(
            history,
            (),
            RunState(original_task="original task"),
        )


def test_program_observation_stays_attached_to_preceding_atomic_group() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant("one", reasoning="reasoning"))
    history.add_tool("one", '{"ok":true}')
    history.add_user("PROGRAM_NO_PROGRESS")
    history.add_assistant(_assistant("two", reasoning="latest"))
    history.add_tool("two", '{"ok":true}')
    state = RunState(original_task="task")
    full = ContextManager(budget_bytes=100_000, target_ratio=1.0).build(history, (), state)

    view = ContextManager(budget_bytes=full.byte_count - 1, target_ratio=1.0).build(
        history,
        (),
        state,
    )

    assert view.compressed_group_count == 1
    assert all(message.get("content") != "PROGRAM_NO_PROGRESS" for message in view.messages)
    assert any(message.get("tool_call_id") == "two" for message in view.messages)


def test_newest_multi_call_group_is_kept_with_all_results() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    history.add_assistant(_assistant("old", reasoning="old reasoning"))
    history.add_tool("old", json.dumps({"ok": True, "data": {"text": "x" * 5000}}))
    history.add_assistant(
        AssistantMessage(
            content=None,
            reasoning_content="latest reasoning",
            finish_reason="tool_calls",
            usage=None,
            tool_calls=(
                ToolCall(id="a", function=FunctionCall(name="probe", arguments='{"n":1}')),
                ToolCall(id="b", function=FunctionCall(name="probe", arguments='{"n":2}')),
            ),
        )
    )
    history.add_tool("a", '{"ok":true,"data":{"n":1}}')
    history.add_tool("b", '{"ok":true,"data":{"n":2}}')
    state = RunState(original_task="task")
    full = ContextManager(budget_bytes=100_000, target_ratio=1.0).build(history, (), state)

    view = ContextManager(budget_bytes=full.byte_count - 1, target_ratio=1.0).build(
        history,
        (),
        state,
    )

    assert view.compressed_group_count == 1
    assert [message["tool_call_id"] for message in view.messages if message["role"] == "tool"] == [
        "a",
        "b",
    ]
    assistant = next(message for message in view.messages if message["role"] == "assistant")
    assert [call["id"] for call in assistant["tool_calls"]] == ["a", "b"]
    assert "old reasoning" not in str(view.messages[0]["content"])
    assert "x" * 100 not in str(view.messages[0]["content"])


def test_tool_schemas_are_included_in_budget_measurement() -> None:
    history = MessageHistory()
    history.add_system("system")
    history.add_user("task")
    state = RunState(original_task="task")
    base = ContextManager(budget_bytes=100_000).build(history, (), state)
    manager = ContextManager(budget_bytes=base.byte_count + 100)
    large_tools = (
        {
            "type": "function",
            "function": {
                "name": "large",
                "description": "x" * 1000,
                "parameters": {"type": "object"},
            },
        },
    )

    assert manager.build(history, (), state).byte_count <= base.byte_count + 100
    with pytest.raises(ContextBudgetError):
        manager.build(history, large_tools, state)
