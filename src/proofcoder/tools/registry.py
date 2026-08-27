"""Local registration, validation, and synchronous tool dispatch."""

from __future__ import annotations

import json
from copy import deepcopy

from proofcoder.errors import ToolRegistrationError
from proofcoder.protocol import ToolCall
from proofcoder.tools.base import PreparedToolCall, ToolDefinition, ToolResult


class ToolRegistry:
    """Own the complete set of tools available to one AgentLoop."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        """Register one uniquely named tool."""

        if definition.name in self._definitions:
            raise ToolRegistrationError(f"Tool name is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def schemas(self) -> list[dict[str, object]]:
        """Export registered schemas in deterministic registration order."""

        return [definition.to_openai_schema() for definition in self._definitions.values()]

    def find(self, name: str) -> ToolDefinition | None:
        """Find a registered tool without executing it."""

        return self._definitions.get(name)

    def prepare(self, call: ToolCall) -> PreparedToolCall | ToolResult:
        """Parse and validate a call without producing side effects."""

        definition = self.find(call.function.name)
        if definition is None:
            return ToolResult.failure(
                "UNKNOWN_TOOL",
                f"unknown tool '{call.function.name}'; use a registered tool name",
                retryable=True,
            )
        if call.type != "function":
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                "tool call type must be 'function'",
                retryable=True,
            )

        try:
            decoded = json.loads(call.function.arguments)
        except json.JSONDecodeError:
            return ToolResult.failure(
                "INVALID_JSON",
                "tool arguments must be valid JSON",
                retryable=True,
            )
        if not isinstance(decoded, dict):
            return ToolResult.failure(
                "INVALID_ARGUMENTS",
                "tool arguments must be a JSON object",
                retryable=True,
            )

        arguments = dict(decoded)
        validation_error = _validate_arguments(definition, arguments)
        if validation_error is not None:
            return validation_error
        return PreparedToolCall(call=call, definition=definition, arguments=arguments)

    def execute(self, prepared: PreparedToolCall) -> ToolResult:
        """Execute a previously validated call and contain ordinary exceptions."""

        try:
            result = prepared.definition.execute(prepared.arguments)
            result.to_json()
            return result
        except Exception:
            return ToolResult.failure(
                "TOOL_EXECUTION_ERROR",
                "tool execution failed; inspect arguments and workspace state",
                retryable=True,
            )

    def dispatch(self, call: ToolCall) -> ToolResult:
        """Validate and execute one call for direct non-batch use."""

        prepared = self.prepare(call)
        if isinstance(prepared, ToolResult):
            return prepared
        return self.execute(prepared)


def _validate_arguments(
    definition: ToolDefinition,
    arguments: dict[str, object],
) -> ToolResult | None:
    schema = definition.parameters
    properties_value = schema.get("properties", {})
    required_value = schema.get("required", [])
    properties = properties_value if isinstance(properties_value, dict) else {}
    required = required_value if isinstance(required_value, list) else []

    unknown = sorted(set(arguments) - set(properties))
    if unknown and schema.get("additionalProperties") is False:
        return _invalid_arguments(f"unknown argument: {unknown[0]}")

    for name in required:
        if isinstance(name, str) and name not in arguments:
            return _invalid_arguments(f"missing required argument: {name}")

    for name, property_schema_value in properties.items():
        if not isinstance(name, str) or not isinstance(property_schema_value, dict):
            continue
        if name not in arguments:
            if "default" in property_schema_value:
                arguments[name] = deepcopy(property_schema_value["default"])
            continue

        value = arguments[name]
        allowed_types = property_schema_value.get("type")
        if not _matches_json_type(value, allowed_types):
            return _invalid_arguments(f"argument '{name}' has the wrong type")
        if type(value) is int:
            minimum = property_schema_value.get("minimum")
            maximum = property_schema_value.get("maximum")
            if isinstance(minimum, int) and value < minimum:
                return _invalid_arguments(f"argument '{name}' is below its minimum")
            if isinstance(maximum, int) and value > maximum:
                return _invalid_arguments(f"argument '{name}' is above its maximum")
    return None


def _matches_json_type(value: object, expected: object) -> bool:
    allowed = [expected] if isinstance(expected, str) else expected
    if not isinstance(allowed, list):
        return True
    checks = {
        "null": value is None,
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }
    return any(checks.get(item, False) for item in allowed if isinstance(item, str))


def _invalid_arguments(message: str) -> ToolResult:
    return ToolResult.failure("INVALID_ARGUMENTS", message, retryable=True)
