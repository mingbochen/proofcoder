"""Explicit, environment-backed configuration for ProofCoder."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Self, cast

from proofcoder.errors import ConfigurationError

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"

ReasoningEffort = Literal["low", "high", "max"]
_ALLOWED_REASONING_EFFORTS = frozenset({"low", "high", "max"})


class ProviderName(StrEnum):
    """Which model provider a run talks to.

    Each provider keeps its own environment group. Sharing variable names would make
    "which variable applies right now" a question only the source can answer.
    """

    DEEPSEEK = "deepseek"
    OLLAMA = "ollama"


@dataclass(frozen=True, slots=True)
class ProofCoderConfig:
    """Runtime configuration loaded on demand from a supplied environment."""

    api_key: str | None = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: ReasoningEffort = DEFAULT_REASONING_EFFORT
    provider: ProviderName = ProviderName.DEEPSEEK

    @property
    def requires_api_key(self) -> bool:
        """Return whether this provider needs a credential at all.

        A local provider does not, which is what lets the real-model path be exercised
        in an environment that has no key to give.
        """

        return self.provider is ProviderName.DEEPSEEK

    @classmethod
    def from_env(
        cls,
        *,
        offline: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> Self:
        """Create configuration without reading the environment at import time."""

        source = os.environ if environ is None else environ
        provider = _provider(source.get("PROOFCODER_PROVIDER"))
        if provider is ProviderName.OLLAMA:
            return cls._from_ollama_env(source)
        return cls._from_deepseek_env(source, offline=offline)

    @classmethod
    def _from_deepseek_env(cls, source: Mapping[str, str], *, offline: bool) -> Self:
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
            provider=ProviderName.DEEPSEEK,
        )

    @classmethod
    def _from_ollama_env(cls, source: Mapping[str, str]) -> Self:
        model = source.get("OLLAMA_MODEL")
        if not model or not model.strip():
            # No default model: the installed set is the user's, and guessing a name
            # would fail at the first request with a message about the wrong thing.
            raise ConfigurationError(
                "OLLAMA_MODEL is required when PROOFCODER_PROVIDER is 'ollama'."
            )
        base_url = _value_or_default(source.get("OLLAMA_BASE_URL"), DEFAULT_OLLAMA_BASE_URL)
        return cls(
            api_key=None,
            base_url=base_url.rstrip("/"),
            model=model.strip(),
            reasoning_effort=DEFAULT_REASONING_EFFORT,
            provider=ProviderName.OLLAMA,
        )


def _provider(value: str | None) -> ProviderName:
    if value is None or not value.strip():
        return ProviderName.DEEPSEEK
    try:
        return ProviderName(value.strip().casefold())
    except ValueError:
        allowed = ", ".join(sorted(item.value for item in ProviderName))
        raise ConfigurationError(f"PROOFCODER_PROVIDER must be one of: {allowed}.") from None


def _value_or_default(value: str | None, default: str) -> str:
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default
