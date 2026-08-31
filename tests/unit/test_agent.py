"""Offline integration tests for AgentLoop and ScriptedClient."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from itertools import pairwise
from pathlib import Path

from proofcoder.agent import AgentLoop
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import (
    AssistantMessage,
    CompletionStatus,
    FunctionCall,
    ModelResponse,
    TerminationReason,
    ToolCall,
    ToolMessage,
)
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import create_create_file_tool, create_replace_in_file_tool
from proofcoder.tools.files import create_list_files_tool, create_read_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry
from proofcoder.tools.search import create_search_text_tool

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


def _read_only_registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace))
    registry.register(create_search_text_tool(workspace))
    registry.register(create_read_file_tool(workspace))
    return registry


def _editable_registry(workspace: Path) -> ToolRegistry:
    registry = _read_only_registry(workspace)
    registry.register(create_create_file_tool(workspace))
    registry.register(create_replace_in_file_tool(workspace))
    return registry


def _c3_registry(workspace: Path) -> ToolRegistry:
    registry = _editable_registry(workspace)
    registry.register(
        create_run_command_tool(
            workspace,
            environ={
                "PATH": str(Path(sys.executable).resolve().parent),
                "PATHEXT": os.environ.get("PATHEXT", ".EXE;.COM"),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
                "TEMP": str(workspace),
                "TMP": str(workspace),
                "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            },
        )
    )
    registry.register(create_finish_task_tool(workspace))
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
            _response(content="I found the workspace listing."),
        ]
    )

    result = _loop(tmp_path, client).run("inspect the workspace")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.final_text == "I found the workspace listing."
    assert result.model_call_count == 3
    assert result.tool_call_count == 1
    assert result.tool_error_count == 0
    assert len(client.requests) == 3
    assert client.requests[0].tools[0]["function"]["name"] == "list_files"

    second_messages = client.requests[1].messages
    assistant = second_messages[2]
    tool = second_messages[3]
    assert assistant["reasoning_content"] == REASONING_SENTINEL
    assert assistant["tool_calls"][0]["id"] == "list-1"
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "list-1"
    assert json.loads(tool["content"])["ok"] is True


def test_search_then_read_results_form_complete_three_request_history(tmp_path: Path) -> None:
    (tmp_path / "agent.py").write_text("class AgentLoop:\n    pass\n", encoding="utf-8")
    client = ScriptedClient(
        [
            _response(
                content="Locating the implementation.",
                calls=(_call("search-1", '{"query":"AgentLoop"}', name="search_text"),),
            ),
            _response(
                content="Reading the relevant segment.",
                calls=(
                    _call(
                        "read-1",
                        '{"path":"agent.py","start_line":1,"end_line":2}',
                        name="read_file",
                    ),
                ),
            ),
            _response(content="AgentLoop is defined in agent.py."),
            _response(content="AgentLoop is defined in agent.py."),
        ]
    )

    result = _loop(tmp_path, client, registry=_read_only_registry(tmp_path)).run("find it")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.tool_call_count == 2
    assert result.tool_error_count == 0
    assert len(client.requests) == 4
    assert [tool["function"]["name"] for tool in client.requests[0].tools] == [
        "list_files",
        "search_text",
        "read_file",
    ]
    second_messages = client.requests[1].messages
    assert second_messages[2]["reasoning_content"] == REASONING_SENTINEL
    assert second_messages[3]["tool_call_id"] == "search-1"
    third_messages = client.requests[2].messages
    assert third_messages[4]["reasoning_content"] == REASONING_SENTINEL
    assert third_messages[5]["tool_call_id"] == "read-1"
    assert "1: class AgentLoop:" in json.loads(third_messages[5]["content"])["data"]["content"]


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
    result = _loop(
        tmp_path,
        ScriptedClient([_response(content="visible"), _response(content="visible")]),
    ).run("task")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.final_text == "visible"
    assert result.tool_call_count == 0
    assert result.model_call_count == 2
    assert result.warnings == ("PROTOCOL_REPAIR",)


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
    assert result.model_call_count == 1
    assert result.api_attempt_count == 2
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


def test_search_read_create_read_replace_read_flow_is_protocol_complete(tmp_path: Path) -> None:
    (tmp_path / "seed.txt").write_text("reference", encoding="utf-8")
    calls = (
        _call("search", '{"query":"reference"}', name="search_text"),
        _call("read-seed", '{"path":"seed.txt"}', name="read_file"),
        _call(
            "create",
            '{"path":"result.txt","content":"old\\n"}',
            name="create_file",
        ),
        _call("read-created", '{"path":"result.txt"}', name="read_file"),
        _call(
            "replace",
            '{"path":"result.txt","old_text":"old","new_text":"new"}',
            name="replace_in_file",
        ),
        _call("read-replaced", '{"path":"result.txt"}', name="read_file"),
    )
    client = ScriptedClient(
        [
            *(_response(calls=(call,)) for call in calls),
            _response(content="Modified, unverified."),
            _response(content="Modified, unverified."),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=_editable_registry(tmp_path),
        max_steps=8,
    ).run("create and update result")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.tool_call_count == 6
    assert result.tool_error_count == 0
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "new\n"
    tool_messages = [
        message for message in result.history.messages if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [call.id for call in calls]
    assistants = [
        message for message in result.history.messages if isinstance(message, AssistantMessage)
    ]
    assert all(message.reasoning_content == REASONING_SENTINEL for message in assistants)
    assert [tool["function"]["name"] for tool in client.requests[0].tools] == [
        "list_files",
        "search_text",
        "read_file",
        "create_file",
        "replace_in_file",
    ]
    final_read = json.loads(client.requests[-2].messages[-1]["content"])
    assert "1: new" in final_read["data"]["content"]


def test_invalid_create_batch_has_no_write_side_effect(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call(
                        "valid",
                        '{"path":"would-exist.txt","content":"data"}',
                        name="create_file",
                    ),
                    _call(
                        "invalid",
                        '{"path":"other.txt","content":"data","unknown":true}',
                        name="create_file",
                    ),
                )
            ),
            _response(content="corrected"),
        ]
    )

    result = _loop(tmp_path, client, registry=_editable_registry(tmp_path)).run("batch")

    assert not (tmp_path / "would-exist.txt").exists()
    assert not (tmp_path / "other.txt").exists()
    assert [_payload["error"]["code"] for _payload in _tool_payloads(result)] == [
        "BATCH_REJECTED",
        "INVALID_ARGUMENTS",
    ]


def test_duplicate_create_ids_have_no_write_side_effect(tmp_path: Path) -> None:
    duplicate = _call(
        "same",
        '{"path":"duplicate.txt","content":"data"}',
        name="create_file",
    )
    client = ScriptedClient(
        [_response(calls=(duplicate, duplicate)), _response(content="corrected")]
    )

    result = _loop(tmp_path, client, registry=_editable_registry(tmp_path)).run("duplicates")

    assert not (tmp_path / "duplicate.txt").exists()
    assert {payload["error"]["code"] for payload in _tool_payloads(result)} == {
        "DUPLICATE_TOOL_CALL_ID"
    }


def test_agent_recovers_from_existing_create_target(tmp_path: Path) -> None:
    (tmp_path / "existing.txt").write_text("original", encoding="utf-8")
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call(
                        "existing",
                        '{"path":"existing.txt","content":"new"}',
                        name="create_file",
                    ),
                )
            ),
            _response(
                calls=(
                    _call(
                        "alternative",
                        '{"path":"alternative.txt","content":"new"}',
                        name="create_file",
                    ),
                )
            ),
            _response(content="used another path"),
        ]
    )

    result = _loop(tmp_path, client, registry=_editable_registry(tmp_path)).run("create")

    assert _tool_payloads(result)[0]["error"]["code"] == "PATH_ALREADY_EXISTS"
    assert (tmp_path / "existing.txt").read_text(encoding="utf-8") == "original"
    assert (tmp_path / "alternative.txt").read_text(encoding="utf-8") == "new"


def test_agent_reads_more_context_after_ambiguous_replace_and_retries(tmp_path: Path) -> None:
    target = tmp_path / "values.txt"
    target.write_text("old and old", encoding="utf-8")
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call(
                        "ambiguous",
                        '{"path":"values.txt","old_text":"old","new_text":"new"}',
                        name="replace_in_file",
                    ),
                )
            ),
            _response(calls=(_call("read", '{"path":"values.txt"}', name="read_file"),)),
            _response(
                calls=(
                    _call(
                        "retry",
                        '{"path":"values.txt","old_text":"old","new_text":"new",'
                        '"expected_replacements":2}',
                        name="replace_in_file",
                    ),
                )
            ),
            _response(content="updated both occurrences"),
        ]
    )

    result = _loop(tmp_path, client, registry=_editable_registry(tmp_path)).run("replace")

    assert _tool_payloads(result)[0]["error"]["code"] == "AMBIGUOUS_MATCH"
    assert target.read_text(encoding="utf-8") == "new and new"


def test_blocked_command_rejects_same_batch_write_before_any_side_effect(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _response(
                calls=(
                    _call(
                        "write",
                        '{"path":"must-not-exist.txt","content":"data"}',
                        name="create_file",
                    ),
                    _call(
                        "blocked",
                        '{"argv":["python","-c","print(1)"]}',
                        name="run_command",
                    ),
                )
            ),
            _response(content="selected a safer approach"),
        ]
    )

    result = _loop(tmp_path, client, registry=_c3_registry(tmp_path)).run("unsafe batch")

    assert not (tmp_path / "must-not-exist.txt").exists()
    payloads = _tool_payloads(result)
    assert [payload["error"]["code"] for payload in payloads] == [
        "BATCH_REJECTED",
        "COMMAND_BLOCKED",
    ]
    assert result.tool_error_count == 2


def test_duplicate_command_ids_do_not_execute_any_command(tmp_path: Path) -> None:
    (tmp_path / "marker.py").write_text(
        "from pathlib import Path\nPath('marker.txt').write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    duplicate = _call(
        "duplicate-command",
        '{"argv":["python","marker.py"]}',
        name="run_command",
    )
    client = ScriptedClient(
        [_response(calls=(duplicate, duplicate)), _response(content="used unique IDs")]
    )

    result = _loop(tmp_path, client, registry=_c3_registry(tmp_path)).run("duplicate IDs")

    assert not (tmp_path / "marker.txt").exists()
    assert not (tmp_path / ".proofcoder").exists()
    assert {payload["error"]["code"] for payload in _tool_payloads(result)} == {
        "DUPLICATE_TOOL_CALL_ID"
    }


def test_scripted_failure_read_edit_success_flow_preserves_protocol(tmp_path: Path) -> None:
    (tmp_path / "check.py").write_text(
        "import subject\n"
        "print(f'VALUE={subject.VALUE}')\n"
        "raise SystemExit(0 if subject.VALUE == 'fixed' else 1)\n",
        encoding="utf-8",
    )
    (tmp_path / "subject.py").write_text('VALUE = "broken"\n', encoding="utf-8")
    command_arguments = '{"argv":["python","check.py"],"timeout_seconds":10}'
    calls = (
        _call("read-test", '{"path":"check.py"}', name="read_file"),
        _call("run-failing", command_arguments, name="run_command"),
        _call("read-source", '{"path":"subject.py"}', name="read_file"),
        _call(
            "edit-source",
            '{"path":"subject.py","old_text":"broken","new_text":"fixed"}',
            name="replace_in_file",
        ),
        _call("run-passing", command_arguments, name="run_command"),
    )
    final_text = "Ran python check.py: exit 1; after the exact edit, python check.py: exit 0."
    client = ScriptedClient(
        [
            *(_response(calls=(call,)) for call in calls),
            _response(content=final_text),
            _response(content=final_text),
        ]
    )

    result = _loop(
        tmp_path,
        client,
        registry=_c3_registry(tmp_path),
        max_steps=7,
    ).run("fix the failing check and report the real exit codes")

    assert result.termination_reason is TerminationReason.MODEL_STOPPED
    assert result.final_text == final_text
    assert result.tool_call_count == 5
    assert result.tool_error_count == 0
    assert (tmp_path / "subject.py").read_text(encoding="utf-8") == 'VALUE = "fixed"\n'
    payloads = _tool_payloads(result)
    assert payloads[1]["data"]["exit_code"] == 1
    assert payloads[1]["data"]["stdout"] == "VALUE=broken\n"
    assert payloads[4]["data"]["exit_code"] == 0
    assert payloads[4]["data"]["stdout"] == "VALUE=fixed\n"
    tool_messages = [
        message for message in result.history.messages if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [call.id for call in calls]
    assistants = [
        message for message in result.history.messages if isinstance(message, AssistantMessage)
    ]
    assert all(message.reasoning_content == REASONING_SENTINEL for message in assistants)
    assert [tool["function"]["name"] for tool in client.requests[0].tools] == [
        "list_files",
        "search_text",
        "read_file",
        "create_file",
        "replace_in_file",
        "run_command",
        "finish_task",
    ]


def test_read_modify_fail_modify_pass_finish_is_locally_verified(tmp_path: Path) -> None:
    (tmp_path / "subject.py").write_text('VALUE = "initial"\n', encoding="utf-8")
    (tmp_path / "test_subject.py").write_text(
        "import unittest\n"
        "import subject\n\n"
        "class SubjectTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(subject.VALUE, 'fixed')\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    verification_arguments = ["python", "-m", "unittest", "-q"]
    calls = (
        _call("read", '{"path":"subject.py"}', name="read_file"),
        _call(
            "first-edit",
            '{"path":"subject.py","old_text":"initial","new_text":"broken"}',
            name="replace_in_file",
        ),
        _call(
            "failing-test",
            json.dumps({"argv": verification_arguments, "timeout_seconds": 10}),
            name="run_command",
        ),
        _call(
            "second-edit",
            '{"path":"subject.py","old_text":"broken","new_text":"fixed"}',
            name="replace_in_file",
        ),
        _call(
            "passing-test",
            json.dumps({"argv": verification_arguments, "timeout_seconds": 10}),
            name="run_command",
        ),
        _call(
            "finish",
            json.dumps(
                {
                    "summary": "fixed the value",
                    "changed_files": ["subject.py"],
                    "verification_command": verification_arguments,
                    "limitations": [],
                    "blocked_reason": None,
                }
            ),
            name="finish_task",
        ),
    )
    client = ScriptedClient([*(_response(calls=(call,)) for call in calls)])

    result = _loop(
        tmp_path,
        client,
        registry=_c3_registry(tmp_path),
        max_steps=8,
    ).run("fix and verify")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.completion_status is CompletionStatus.COMPLETED_VERIFIED
    assert result.changed_files == ("subject.py",)
    assert result.verification_command == tuple(verification_arguments)
    assert result.verification_cwd == "."
    assert result.verification_exit_code == 0
    assert result.final_report is not None
    assert "exit_code=1" in result.final_report
    assert "exit_code=0" in result.final_report
    assert "Valid verification after latest modification: event=5" in result.final_report
    assert len(client.requests) == len(calls)
    assert result.model_call_count == len(calls)
    assert result.tool_call_count == len(calls)
    assert result.tool_error_count == 0
    tool_messages = [
        message for message in result.history.messages if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [call.id for call in calls]
    assistants = [
        message for message in result.history.messages if isinstance(message, AssistantMessage)
    ]
    assert all(message.reasoning_content == REASONING_SENTINEL for message in assistants)


def test_modified_then_finish_is_unverified_and_uses_actual_path(tmp_path: Path) -> None:
    create = _call(
        "create",
        '{"path":"actual.txt","content":"content"}',
        name="create_file",
    )
    finish = _call(
        "finish",
        json.dumps(
            {
                "summary": "created a file",
                "changed_files": ["claimed.txt"],
                "verification_command": ["python", "-m", "pytest", "-q"],
            }
        ),
        name="finish_task",
    )
    client = ScriptedClient([_response(calls=(create,)), _response(calls=(finish,))])

    result = _loop(tmp_path, client, registry=_c3_registry(tmp_path)).run("create")

    assert result.completion_status is CompletionStatus.COMPLETED_UNVERIFIED
    assert result.changed_files == ("actual.txt",)
    assert result.verification_command is None
    assert result.final_report is not None
    assert "MODEL_CHANGED_FILES_MISMATCH" in result.final_report
    assert "MODEL_VERIFICATION_UNCONFIRMED" in result.final_report


def test_finish_mixed_batch_rejects_all_calls_without_side_effect(tmp_path: Path) -> None:
    create = _call(
        "create",
        '{"path":"must-not-exist.txt","content":"content"}',
        name="create_file",
    )
    mixed_finish = _call(
        "mixed-finish",
        '{"summary":"done","changed_files":[]}',
        name="finish_task",
    )
    sole_finish = _call(
        "sole-finish",
        '{"summary":"nothing changed","changed_files":[]}',
        name="finish_task",
    )
    client = ScriptedClient(
        [
            _response(calls=(create, mixed_finish)),
            _response(calls=(sole_finish,)),
        ]
    )

    result = _loop(tmp_path, client, registry=_c3_registry(tmp_path)).run("batch")

    assert not (tmp_path / "must-not-exist.txt").exists()
    assert result.completion_status is CompletionStatus.COMPLETED_NO_CHANGES
    payloads = _tool_payloads(result)
    assert [payload["error"]["code"] for payload in payloads[:2]] == [
        "BATCH_REJECTED",
        "FINISH_TASK_MUST_BE_SOLE_CALL",
    ]
    assert payloads[2]["data"]["completion_status"] == "completed_no_changes"
    assert result.tool_error_count == 2
    tool_messages = [
        message for message in result.history.messages if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "create",
        "mixed-finish",
        "sole-finish",
    ]


def test_finish_blocked_stops_without_requesting_another_model_response(tmp_path: Path) -> None:
    finish = _call(
        "finish",
        '{"summary":"blocked","blocked_reason":"required input unavailable"}',
        name="finish_task",
    )
    client = ScriptedClient([_response(calls=(finish,))])

    result = _loop(tmp_path, client, registry=_c3_registry(tmp_path)).run("blocked")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.completion_status is CompletionStatus.BLOCKED
    assert len(client.requests) == 1


def _truncated_response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    """Build a response the provider cut off at the output token ceiling."""

    return ModelResponse(
        content=content,
        reasoning_content=REASONING_SENTINEL,
        finish_reason="length",
        usage=None,
        tool_calls=tuple(calls),
    )


def _user_contents(result) -> list[str]:
    return [
        str(message["content"])
        for message in result.history.to_api_messages()
        if message.get("role") == "user"
    ]


def test_truncated_tool_arguments_produce_program_guidance_not_only_a_parse_error(
    tmp_path: Path,
) -> None:
    # A create_file call cut off mid-argument: the JSON never closes.
    truncated = _call(
        "cut-off",
        '{"path":"big.py","content":"line one\nline two',
        name="create_file",
    )
    client = ScriptedClient(
        [
            _truncated_response(truncated),
            _response(content="understood"),
            _response(content="understood"),
        ]
    )

    result = _loop(tmp_path, client, registry=_editable_registry(tmp_path)).run("write a big file")

    payloads = _tool_payloads(result)
    # The protocol invariant still holds: the truncated call gets its matching result.
    assert len(payloads) == 1
    assert payloads[0]["error"]["code"] == "INVALID_JSON"
    assert "OUTPUT_TRUNCATED" in result.warnings
    guidance = [text for text in _user_contents(result) if "PROGRAM_OUTPUT_TRUNCATED" in text]
    assert len(guidance) == 1
    assert "replace_in_file" in guidance[0]
    assert not (tmp_path / "big.py").exists()


def test_truncated_guidance_follows_the_tool_results_and_keeps_groups_atomic(
    tmp_path: Path,
) -> None:
    client = ScriptedClient(
        [
            _truncated_response(_call("listing")),
            _response(content="understood"),
            _response(content="understood"),
        ]
    )

    result = _loop(tmp_path, client).run("list the workspace")

    roles = [str(message.get("role")) for message in result.history.to_api_messages()]
    assistant_index = roles.index("assistant")
    tool_index = roles.index("tool")
    guidance_index = max(
        index
        for index, message in enumerate(result.history.to_api_messages())
        if message.get("role") == "user" and "PROGRAM_OUTPUT_TRUNCATED" in str(message["content"])
    )
    # An assistant tool_calls message must be followed by its results before any
    # user message is allowed to interrupt the group.
    assert assistant_index < tool_index < guidance_index


def test_truncated_response_without_tool_calls_explains_the_ceiling(tmp_path: Path) -> None:
    client = ScriptedClient(
        [
            _truncated_response(content="a very long answer that ran out of room"),
            _response(content="done"),
        ]
    )

    result = _loop(tmp_path, client).run("explain at length")

    guidance = [text for text in _user_contents(result) if "PROGRAM_OUTPUT_TRUNCATED" in text]
    assert len(guidance) == 1
    assert "OUTPUT_TRUNCATED" in result.warnings
    assert result.termination_reason is TerminationReason.MODEL_STOPPED


def test_untruncated_responses_never_add_the_guidance(tmp_path: Path) -> None:
    client = ScriptedClient([_response(calls=(_call("only"),)), _response(content="done")])

    result = _loop(tmp_path, client).run("ordinary run")

    assert not any("PROGRAM_OUTPUT_TRUNCATED" in text for text in _user_contents(result))
    assert "OUTPUT_TRUNCATED" not in result.warnings


def test_no_progress_and_truncation_notes_combine_into_one_user_turn(tmp_path: Path) -> None:
    # Two identical truncated batches: the repeat trips no-progress on the second,
    # while truncation guidance is also owed. The history rejects consecutive user
    # turns, so both must arrive as a single message.
    client = ScriptedClient(
        [
            _truncated_response(_call("first")),
            _truncated_response(_call("second")),
            _response(content="understood"),
            _response(content="understood"),
        ]
    )

    result = _loop(tmp_path, client).run("repeat the same listing")

    messages = result.history.to_api_messages()
    roles = [str(message.get("role")) for message in messages]
    combined = [
        str(message["content"])
        for message in messages
        if message.get("role") == "user" and "PROGRAM_OUTPUT_TRUNCATED" in str(message["content"])
    ]
    assert len(combined) == 2
    assert "PROGRAM_NO_PROGRESS" in combined[-1]
    assert "PROGRAM_OUTPUT_TRUNCATED" in combined[-1]
    # No two user turns may ever be adjacent.
    assert not any(first == second == "user" for first, second in pairwise(roles))
