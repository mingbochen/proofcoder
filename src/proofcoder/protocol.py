"""Stage A response models owned by ProofCoder."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token counts reported by a Chat Completions response."""

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Normalized response fields required by ProofCoder."""

    content: str | None
    reasoning_content: str | None
    finish_reason: str | None
    usage: TokenUsage | None
