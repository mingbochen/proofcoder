"""Local tool definitions and deterministic result envelopes."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from proofcoder.protocol import ToolCall


class RiskLevel(StrEnum):
    """Tool risk labels used by local safety and presentation layers."""

    READ_ONLY = "read_only"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ToolError:
    """Structured, model-actionable tool failure."""

    code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible mapping."""

        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class ToolMeta:
    """Small common metadata attached to every tool result."""

    duration_ms: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic JSON-compatible mapping."""

        return {"duration_ms": self.duration_ms, "truncated": self.truncated}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Uniform JSON-serializable success or failure envelope."""

    ok: bool
    data: dict[str, object] | None
    error: ToolError | None
    meta: ToolMeta = field(default_factory=ToolMeta)

    @classmethod
    def success(
        cls,
        data: dict[str, object],
        *,
        truncated: bool = False,
    ) -> ToolResult:
        """Build a successful tool result."""

        return cls(ok=True, data=data, error=None, meta=ToolMeta(truncated=truncated))

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> ToolResult:
        """Build a structured failure without including exception details."""

        return cls(
            ok=False,
            data=None,
            error=ToolError(code=code, message=message, retryable=retryable),
        )

    def to_dict(self) -> dict[str, object]:
        """Return the envelope using a fixed top-level key order."""

        return {
            "ok": self.ok,
            "data": self.data,
            "error": None if self.error is None else self.error.to_dict(),
            "meta": self.meta.to_dict(),
        }

    def to_json(self) -> str:
        """Serialize the result deterministically, never as a Python repr."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )


ToolExecutor = Callable[[Mapping[str, object]], ToolResult]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """One locally validated and executed function tool."""

    name: str
    description: str
    parameters: dict[str, object]
    execute: ToolExecutor = field(repr=False)
    modifies_workspace: bool = False
    risk_level: RiskLevel = RiskLevel.READ_ONLY

    def to_openai_schema(self) -> dict[str, object]:
        """Export a stable OpenAI-compatible function tool schema."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class PreparedToolCall:
    """A tool call whose name and arguments passed local validation."""

    call: ToolCall
    definition: ToolDefinition
    arguments: dict[str, object]
