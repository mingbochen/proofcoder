"""Offline integration tests for AgentLoop and ScriptedClient."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from proofcoder.agent import AgentLoop
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import (
    FunctionCall,
    ModelResponse,
    TerminationReason,
    ToolCall,
    ToolMessage,
)
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.files import create_list_files_tool
from proofcoder.tools.registry import ToolRegistry

REASONING_SENTINEL = "reasoning-remains-in-history-only"


def _response(
    *,
    content: str | None = None,
    calls: tuple[ToolCall, ...] = (),
) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content=REASONING_SENTINEL,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _call(call_id: str, arguments: str = "{}", *, name: str = "list_files") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name=name, arguments=arguments),
    )


def _list_registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace))
    return registry


def _loop(
    workspace: Path,
    client: ScriptedClient,
    *,
    registry: ToolRegistry | None = None,
    max_steps: int = 4,
) -> AgentLoop:
    return AgentLoop(
        client=client,
        registry=_list_registry(workspace) if registry is None else registry,
        workspace=workspace,
        system_prompt="test system",
        max_steps=max_steps,
    )


def _tool_payloads(result) -> list[dict[str, object]]:
    return [
        json.loads(message.content)
        for message in result.history.messages
        if isinstance(message, ToolMessage)
    ]


def test_list_files_result_is_returned_to_second_model_call(tmp_path: Path) -> None:
    (tmp_path / "visible.py").write_text("", encoding="utf-8")
    client = ScriptedClient(
        [
            _response(calls=(_call("list-1"),)),
            _response(content="I found the workspace listing."),
        ]
    )

    result = _loop(tmp_path, client).run("inspect the workspace")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.final_text == "I found the workspace listing."
    assert result.model_call_count == 2
    assert result.tool_call_count == 1
    assert result.tool_error_count == 0
    assert len(client.requests) == 2
    assert client.requests[0].tools[0]["function"]["name"] == "list_files"

    second_messages = client.requests[1].messages
    assistant = second_messages[2]
    tool = second_messages[3]
    assert assistant["reasoning_content"] == REASONING_SENTINEL
    assert assistant["tool_calls"][0]["id"] == "list-1"
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "list-1"
    assert json.loads(tool["content"])["ok"] is True


def test_multiple_valid_calls_execute_synchronously_in_model_order(tmp_path: Path) -> None:
    execution_order: list[str] = []

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        value = str(arguments["value"])
        execution_order.append(value)
        return ToolResult.success({"value": value})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="ordered",
            description="Record execution order.",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            execute=execute,
        )
    )
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call("one", '{"value":"first"}', name="ordered"),
                    _call("two", '{"value":"second"}', name="ordered"),
                )
            ),
            _response(content="done"),
        ]
    )

    result = _loop(tmp_path, client, registry=registry).run("order")

    assert execution_order == ["first", "second"]
    assert result.tool_call_count == 2
    assert [payload["data"]["value"] for payload in _tool_payloads(result)] == [
        "first",
        "second",
    ]


def test_invalid_batch_rejects_every_call_before_execution(tmp_path: Path) -> None:
    executed = False

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        nonlocal executed
        executed = True
        return ToolResult.success({})

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample",
            description="Never execute in an invalid batch.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=execute,
        )
    )
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call("valid", "{}", name="sample"),
                    _call("invalid", "{", name="sample"),
                )
            ),
            _response(content="recovered"),
        ]
    )

    result = _loop(tmp_path, client, registry=registry).run("batch")

    assert executed is False
    payloads = _tool_payloads(result)
    assert payloads[0]["error"]["code"] == "BATCH_REJECTED"
    assert payloads[1]["error"]["code"] == "INVALID_JSON"
    assert result.tool_error_count == 2


def test_unknown_tool_gets_structured_result_and_loop_continues(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("unknown", "{}", name="not_registered"),)),
            _response(content="corrected"),
        ]
    )

    result = _loop(tmp_path, client).run("unknown")

    assert _tool_payloads(result)[0]["error"]["code"] == "UNKNOWN_TOOL"
    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.tool_error_count == 1


def test_tool_exception_is_returned_without_stopping_other_protocol_steps(tmp_path: Path) -> None:
    def fail(arguments: Mapping[str, object]) -> ToolResult:
        raise RuntimeError(arguments)

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="failing",
            description="Fail safely.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            execute=fail,
        )
    )
    client = ScriptedClient(
        [
            _response(calls=(_call("fail", "{}", name="failing"),)),
            _response(content="observed failure"),
        ]
    )

    result = _loop(tmp_path, client, registry=registry).run("failure")

    assert _tool_payloads(result)[0]["error"]["code"] == "TOOL_EXECUTION_ERROR"
    assert result.final_text == "observed failure"


def test_response_without_tool_calls_stops_controlled(tmp_path: Path) -> None:
    result = _loop(tmp_path, ScriptedClient([_response(content="visible")])).run("task")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.final_text == "visible"
    assert result.tool_call_count == 0


def test_max_steps_stops_after_processing_last_tool_group(tmp_path: Path) -> None:
    client = ScriptedClient([_response(calls=(_call("only"),))])

    result = _loop(tmp_path, client, max_steps=1).run("bounded")

    assert result.termination_reason is TerminationReason.MAX_STEPS
    assert result.model_call_count == 1
    assert len(_tool_payloads(result)) == 1


def test_script_exhaustion_becomes_controlled_api_error(tmp_path: Path) -> None:
    client = ScriptedClient([_response(calls=(_call("first"),))])

    result = _loop(tmp_path, client, max_steps=2).run("exhaust")

    assert result.termination_reason is TerminationReason.API_ERROR
    assert result.model_call_count == 2
    assert len(client.requests) == 2


def test_duplicate_ids_reject_whole_batch_with_one_result_per_call(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(calls=(_call("duplicate"), _call("duplicate"))),
            _response(content="fixed"),
        ]
    )

    result = _loop(tmp_path, client).run("duplicates")

    payloads = _tool_payloads(result)
    assert len(payloads) == 2
    assert {payload["error"]["code"] for payload in payloads} == {"DUPLICATE_TOOL_CALL_ID"}
    assert result.tool_error_count == 2
