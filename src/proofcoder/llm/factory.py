"""One place that turns configuration into a client.

Every caller that needs a model client goes through here, so adding a provider never
means teaching the CLI, the browser or the evaluation runner about it.
"""

from __future__ import annotations

from typing import Protocol

from proofcoder.config import ProofCoderConfig, ProviderName
from proofcoder.llm.base import LLMClient
from proofcoder.llm.deepseek import DeepSeekClient
from proofcoder.llm.ollama import OllamaClient
from proofcoder.protocol import ModelResponse


class ConnectivityClient(Protocol):
    """A client that can answer whether its provider is reachable."""

    def check_connection(self) -> ModelResponse: ...


def create_client(config: ProofCoderConfig) -> LLMClient:
    """Create the model client this configuration selects."""

    if config.provider is ProviderName.OLLAMA:
        return OllamaClient(config)
    return DeepSeekClient(config)


def create_connectivity_client(config: ProofCoderConfig) -> ConnectivityClient:
    """Create the client `doctor` uses to check one provider's reachability."""

    if config.provider is ProviderName.OLLAMA:
        return OllamaClient(config)
    return DeepSeekClient(config)
