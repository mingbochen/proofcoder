"""Minimal model-client seam used by the Stage B agent loop."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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


class StreamingLLMClient(LLMClient, Protocol):
    """A client that can also deliver one response as a stream.

    Streaming is an optional capability rather than a second required method: a
    provider that cannot stream must still be a perfectly good ``LLMClient``.
    """

    def complete_streaming(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
        *,
        on_text: Callable[[str], None] | None = None,
    ) -> ModelResponse: ...


@dataclass(frozen=True, slots=True)
class StreamingClient:
    """Present a streaming-capable client through the ordinary ``complete`` seam.

    This is what keeps ``AgentLoop`` unaware of streaming entirely: it calls
    ``complete`` as it always has, and whether the bytes arrived in one piece or many
    is settled before the response reaches it.
    """

    inner: StreamingLLMClient
    on_text: Callable[[str], None] | None = None

    def complete(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
    ) -> ModelResponse:
        """Assemble one streamed response and return it like any other."""

        return self.inner.complete_streaming(messages, tools, on_text=self.on_text)


def supports_streaming(client: object) -> bool:
    """Return whether one client can deliver a response as a stream."""

    return callable(getattr(client, "complete_streaming", None))
