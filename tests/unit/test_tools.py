"""Tool result and ToolRegistry validation tests."""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from proofcoder.errors import ToolRegistrationError
from proofcoder.protocol import FunctionCall, ToolCall
from proofcoder.tools.base import ToolDefinition, ToolResult
from proofcoder.tools.registry import ToolRegistry


def _definition(execute=None) -> ToolDefinition:
    def default_execute(arguments: Mapping[str, object]) -> ToolResult:
        return ToolResult.success({"arguments": dict(arguments)})

    return ToolDefinition(
        name="sample",
        description="Validate a bounded integer and optional label.",
        parameters={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "label": {"type": "string", "default": "default"},
            },
            "required": ["count"],
            "additionalProperties": False,
        },
        execute=default_execute if execute is None else execute,
    )


def _call(arguments: str, *, name: str = "sample", call_type: str = "function") -> ToolCall:
    return ToolCall(
        id="call-1",
        type=call_type,
        function=FunctionCall(name=name, arguments=arguments),
    )


def _registry(execute=None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_definition(execute))
    return registry


def _error_code(result: ToolResult) -> str:
    assert result.error is not None
    return result.error.code


def test_tool_result_json_has_deterministic_envelope_order() -> None:
    result = ToolResult.success({"value": 1}, truncated=True)

    assert result.to_json() == (
        '{"ok":true,"data":{"value":1},"error":null,"meta":{"duration_ms":0,"truncated":true}}'
    )
    assert json.loads(result.to_json()) == result.to_dict()


def test_duplicate_tool_registration_is_rejected() -> None:
    registry = _registry()

    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(_definition())


def test_schema_export_is_openai_function_shape() -> None:
    schema = _registry().schemas()[0]

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "sample"
    assert "strict" not in schema["function"]


def test_unknown_tool_returns_stable_error() -> None:
    result = _registry().dispatch(_call('{"count":1}', name="missing"))

    assert _error_code(result) == "UNKNOWN_TOOL"


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ("{", "INVALID_JSON"),
        ("[]", "INVALID_ARGUMENTS"),
        ("{}", "INVALID_ARGUMENTS"),
        ('{"count":"1"}', "INVALID_ARGUMENTS"),
        ('{"count":true}', "INVALID_ARGUMENTS"),
        ('{"count":1,"unknown":2}', "INVALID_ARGUMENTS"),
        ('{"count":0}', "INVALID_ARGUMENTS"),
        ('{"count":4}', "INVALID_ARGUMENTS"),
    ],
)
def test_invalid_arguments_are_rejected(arguments: str, code: str) -> None:
    result = _registry().dispatch(_call(arguments))

    assert result.ok is False
    assert _error_code(result) == code


def test_valid_arguments_receive_schema_defaults() -> None:
    result = _registry().dispatch(_call('{"count":2}'))

    assert result.ok is True
    assert result.data == {"arguments": {"count": 2, "label": "default"}}


def test_non_function_call_type_is_rejected() -> None:
    result = _registry().dispatch(_call('{"count":1}', call_type="custom"))

    assert _error_code(result) == "INVALID_ARGUMENTS"


def test_execution_exception_becomes_structured_error() -> None:
    def fail(arguments: Mapping[str, object]) -> ToolResult:
        raise RuntimeError(arguments)

    result = _registry(fail).dispatch(_call('{"count":1}'))

    assert _error_code(result) == "TOOL_EXECUTION_ERROR"
    assert "RuntimeError" not in result.to_json()


def test_non_serializable_tool_data_becomes_execution_error() -> None:
    def invalid_result(arguments: Mapping[str, object]) -> ToolResult:
        return ToolResult.success({"invalid": object()})

    result = _registry(invalid_result).dispatch(_call('{"count":1}'))

    assert _error_code(result) == "TOOL_EXECUTION_ERROR"


@pytest.mark.parametrize("control_flow", [KeyboardInterrupt(), SystemExit()])
def test_base_exceptions_are_not_disguised_as_tool_errors(control_flow: BaseException) -> None:
    def stop(arguments: Mapping[str, object]) -> ToolResult:
        raise control_flow

    with pytest.raises(type(control_flow)):
        _registry(stop).dispatch(_call('{"count":1}'))
