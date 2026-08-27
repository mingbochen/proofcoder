"""Minimal model-client seam used by the Stage B agent loop."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from proofcoder.protocol import ModelResponse

ChatMessagePayload = Mapping[str, object]
ToolSchema = Mapping[str, object]


class LLMClient(Protocol):
    """Return one provider-independent assistant response per synchronous call."""

    def complete(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
    ) -> ModelResponse: ...
