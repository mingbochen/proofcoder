"""Assemble a streamed response into the object the rest of ProofCoder uses.

Every rule here exists so that `AgentLoop` cannot tell a streamed response from a
non-streamed one. A stream is a transport detail: it changes when bytes arrive, not
what a run decides. So assembly finishes, and is validated, before anything reaches
the loop, and a failure at any point yields no tool call at all rather than a partial
one -- a half-assembled batch would be indistinguishable from a real request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from proofcoder.errors import LLMErrorCategory, LLMRequestError
from proofcoder.protocol import FunctionCall, ModelResponse, TokenUsage, ToolCall

MAX_STREAM_TOOL_CALLS = 64
MAX_TOOL_ARGUMENT_CHARACTERS = 64 * 1024
MAX_STREAM_TEXT_CHARACTERS = 1024 * 1024

TextCallback = Callable[[str], None]


@dataclass(slots=True)
class _PartialToolCall:
    """One tool call being accumulated across fragments."""

    call_id: str
    name: str
    arguments: str = ""


@dataclass(slots=True)
class StreamAssembler:
    """Accumulate stream fragments and produce one validated response.

    The incremental callback receives visible text only. A half-written tool call is
    not something to show a person: it reads as an operation that has already started,
    when nothing has run and nothing may yet.
    """

    on_text: TextCallback | None = None
    _text: list[str] = field(default_factory=list, init=False)
    _reasoning: list[str] = field(default_factory=list, init=False)
    _text_length: int = field(default=0, init=False)
    _finish_reason: str | None = field(default=None, init=False)
    _usage: TokenUsage | None = field(default=None, init=False)
    _calls: dict[int, _PartialToolCall] = field(default_factory=dict, init=False)
    _order: list[int] = field(default_factory=list, init=False)
    _saw_fragment: bool = field(default=False, init=False)

    @property
    def saw_fragment(self) -> bool:
        """Return whether the stream delivered anything at all."""

        return self._saw_fragment

    def add_text(self, text: str) -> None:
        """Append visible assistant text and report it to the caller."""

        if not text:
            return
        self._saw_fragment = True
        self._text_length += len(text)
        if self._text_length > MAX_STREAM_TEXT_CHARACTERS:
            raise _permanent("the streamed response exceeded the assistant text limit")
        self._text.append(text)
        if self.on_text is not None:
            self.on_text(text)

    def add_reasoning(self, text: str) -> None:
        """Append private reasoning, which is never reported incrementally."""

        if not text:
            return
        self._saw_fragment = True
        self._reasoning.append(text)

    def set_finish_reason(self, reason: str | None) -> None:
        """Record the terminal reason the provider reported."""

        if reason:
            self._saw_fragment = True
            self._finish_reason = reason

    def set_usage(self, usage: TokenUsage | None) -> None:
        """Record token usage if the provider reported any."""

        if usage is not None:
            self._usage = usage

    def add_tool_call_fragment(
        self,
        index: int,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> None:
        """Merge one tool-call fragment by index.

        The identifier and function name are fixed the first time they are seen. A
        later fragment naming something different is a protocol violation, not an
        update: accepting it would mean executing a call nobody can reconstruct from
        the stream.
        """

        self._saw_fragment = True
        existing = self._calls.get(index)
        if existing is None:
            if len(self._calls) >= MAX_STREAM_TOOL_CALLS:
                raise _permanent(
                    f"the streamed response declared more than {MAX_STREAM_TOOL_CALLS} tool calls"
                )
            existing = _PartialToolCall(call_id=call_id or "", name=name or "")
            self._calls[index] = existing
            self._order.append(index)
        else:
            if call_id and existing.call_id and call_id != existing.call_id:
                raise _permanent("a streamed tool call changed its identifier mid-stream")
            if name and existing.name and name != existing.name:
                raise _permanent("a streamed tool call changed its function name mid-stream")
            if call_id and not existing.call_id:
                existing.call_id = call_id
            if name and not existing.name:
                existing.name = name
        if arguments:
            if len(existing.arguments) + len(arguments) > MAX_TOOL_ARGUMENT_CHARACTERS:
                raise _permanent("a streamed tool call exceeded the argument size limit")
            existing.arguments += arguments

    def finish(self) -> ModelResponse:
        """Validate everything accumulated and produce one complete response."""

        if not self._saw_fragment:
            raise LLMRequestError(
                "the streamed response contained no data",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        calls = tuple(self._build_call(index) for index in self._order)
        content = "".join(self._text)
        reasoning = "".join(self._reasoning)
        return ModelResponse(
            content=content or None,
            reasoning_content=reasoning or None,
            finish_reason="tool_calls" if calls else self._finish_reason,
            usage=self._usage,
            tool_calls=calls,
        )

    def _build_call(self, index: int) -> ToolCall:
        partial = self._calls[index]
        if not partial.name:
            raise _permanent("a streamed tool call never named a function")
        arguments = partial.arguments or "{}"
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            # Parsed once, when complete. Anything read from a partial argument string
            # would be a guess, and a guessed argument cannot afterwards be told apart
            # from one the model actually asked for.
            raise LLMRequestError(
                "a streamed tool call did not assemble into valid JSON arguments",
                category=LLMErrorCategory.INVALID_RESPONSE,
            ) from None
        if not isinstance(decoded, dict):
            raise LLMRequestError(
                "a streamed tool call assembled into arguments that are not an object",
                category=LLMErrorCategory.INVALID_RESPONSE,
            )
        return ToolCall(
            id=partial.call_id or f"stream-tool-{index}",
            function=FunctionCall(name=partial.name, arguments=arguments),
        )


def truncated_stream_error() -> LLMRequestError:
    """Return the error for a stream that ended before assembly could complete."""

    return LLMRequestError(
        "the streamed response ended before it was complete",
        category=LLMErrorCategory.CONNECTION,
    )


def _permanent(message: str) -> LLMRequestError:
    return LLMRequestError(message, category=LLMErrorCategory.PERMANENT)
