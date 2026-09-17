"""One place that turns configuration into a client.

Every caller that needs a model client goes through here, so adding a provider never
means teaching the CLI, the browser or the evaluation runner about it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from proofcoder.config import ProofCoderConfig, ProviderName
from proofcoder.errors import ConfigurationError
from proofcoder.llm.base import (
    LLMClient,
    StreamingClient,
    StreamingLLMClient,
    supports_streaming,
)
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.llm.ollama import OllamaClient
from proofcoder.protocol import ModelResponse


class ConnectivityClient(Protocol):
    """A client that can answer whether its provider is reachable."""

    def check_connection(self) -> ModelResponse: ...


def create_client(
    config: ProofCoderConfig,
    *,
    stream: bool = False,
    on_text: Callable[[str], None] | None = None,
) -> LLMClient:
    """Create the model client this configuration selects.

    Streaming is applied by wrapping, not by a branch inside the loop: the caller asks
    for it here and everything downstream keeps calling ``complete``.
    """

    client: LLMClient = (
        OllamaClient(config) if config.provider is ProviderName.OLLAMA else DeepSeekClient(config)
    )
    if not stream:
        return client
    if not supports_streaming(client):
        raise ConfigurationError("the configured provider does not support streaming.")
    return StreamingClient(inner=cast(StreamingLLMClient, client), on_text=on_text)


def create_connectivity_client(config: ProofCoderConfig) -> ConnectivityClient:
    """Create the client `doctor` uses to check one provider's reachability."""

    if config.provider is ProviderName.OLLAMA:
        return OllamaClient(config)
    return DeepSeekClient(config)
