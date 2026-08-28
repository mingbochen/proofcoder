"""Deterministic offline client for protocol and AgentLoop tests."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from proofcoder.errors import LLMRequestError, ScriptedClientError
from proofcoder.llm.base import ChatMessagePayload, ToolSchema
from proofcoder.protocol import ModelResponse


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    """One immutable snapshot received by a ScriptedClient."""

    messages: tuple[dict[str, object], ...]
    tools: tuple[dict[str, object], ...]


class ScriptedClient:
    """Return predefined responses in order without performing I/O."""

    def __init__(self, responses: Sequence[ModelResponse | LLMRequestError]) -> None:
        self._responses = tuple(responses)
        self._next_response = 0
        self.requests: list[RecordedRequest] = []

    def complete(
        self,
        messages: Sequence[ChatMessagePayload],
        tools: Sequence[ToolSchema] = (),
    ) -> ModelResponse:
        """Record plain request data and return the next scripted response."""

        self.requests.append(
            RecordedRequest(
                messages=tuple(deepcopy(dict(message)) for message in messages),
                tools=tuple(deepcopy(dict(tool)) for tool in tools),
            )
        )
        if self._next_response >= len(self._responses):
            raise ScriptedClientError("ScriptedClient response sequence is exhausted.")
        response = self._responses[self._next_response]
        self._next_response += 1
        if isinstance(response, LLMRequestError):
            raise response
        return response
