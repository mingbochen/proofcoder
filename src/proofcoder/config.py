"""Explicit, environment-backed configuration for ProofCoder."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Self, cast

from proofcoder.errors import ConfigurationError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_REASONING_EFFORT = "high"

ReasoningEffort = Literal["low", "high", "max"]
_ALLOWED_REASONING_EFFORTS = frozenset({"low", "high", "max"})


@dataclass(frozen=True, slots=True)
class ProofCoderConfig:
    """Runtime configuration loaded on demand from a supplied environment."""

    api_key: str | None = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: ReasoningEffort = DEFAULT_REASONING_EFFORT

    @classmethod
    def from_env(
        cls,
        *,
        offline: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Create configuration without reading the environment at import time."""

        source = os.environ if environ is None else environ
        raw_api_key = None if offline else source.get("DEEPSEEK_API_KEY")
        api_key = raw_api_key if raw_api_key and raw_api_key.strip() else None
        base_url = _value_or_default(source.get("DEEPSEEK_BASE_URL"), DEFAULT_BASE_URL)
        model = _value_or_default(source.get("DEEPSEEK_MODEL"), DEFAULT_MODEL)
        raw_effort = _value_or_default(
            source.get("DEEPSEEK_REASONING_EFFORT"),
            DEFAULT_REASONING_EFFORT,
        )

        if raw_effort not in _ALLOWED_REASONING_EFFORTS:
            raise ConfigurationError("DEEPSEEK_REASONING_EFFORT must be one of: low, high, max.")
        if not offline and api_key is None:
            raise ConfigurationError("DEEPSEEK_API_KEY is required in online mode.")

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            reasoning_effort=cast(ReasoningEffort, raw_effort),
        )


def _value_or_default(value: str | None, default: str) -> str:
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default
