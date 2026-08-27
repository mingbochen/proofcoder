"""Tests for explicit environment configuration."""

import pytest

from proofcoder.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
    ProofCoderConfig,
)
from proofcoder.errors import ConfigurationError

SENSITIVE_SENTINEL = "never-print-this-value"


def test_offline_defaults_do_not_require_or_read_api_key() -> None:
    config = ProofCoderConfig.from_env(
        offline=True,
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
    )

    assert config.api_key is None
    assert config.base_url == DEFAULT_BASE_URL
    assert config.model == DEFAULT_MODEL
    assert config.reasoning_effort == DEFAULT_REASONING_EFFORT


def test_environment_overrides_all_online_values() -> None:
    config = ProofCoderConfig.from_env(
        environ={
            "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
            "DEEPSEEK_BASE_URL": "https://example.invalid/api",
            "DEEPSEEK_MODEL": "test-model",
            "DEEPSEEK_REASONING_EFFORT": "max",
        }
    )

    assert config.api_key == SENSITIVE_SENTINEL
    assert config.base_url == "https://example.invalid/api"
    assert config.model == "test-model"
    assert config.reasoning_effort == "max"


def test_blank_non_secret_values_use_defaults() -> None:
    config = ProofCoderConfig.from_env(
        offline=True,
        environ={
            "DEEPSEEK_BASE_URL": " ",
            "DEEPSEEK_MODEL": "",
            "DEEPSEEK_REASONING_EFFORT": "  ",
        },
    )

    assert config.base_url == DEFAULT_BASE_URL
    assert config.model == DEFAULT_MODEL
    assert config.reasoning_effort == DEFAULT_REASONING_EFFORT


def test_invalid_reasoning_effort_is_rejected_without_secret() -> None:
    with pytest.raises(ConfigurationError) as captured:
        ProofCoderConfig.from_env(
            environ={
                "DEEPSEEK_API_KEY": SENSITIVE_SENTINEL,
                "DEEPSEEK_REASONING_EFFORT": "medium",
            }
        )

    assert "low, high, max" in str(captured.value)
    assert "medium" not in str(captured.value)
    assert SENSITIVE_SENTINEL not in str(captured.value)


def test_online_mode_requires_api_key() -> None:
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY is required"):
        ProofCoderConfig.from_env(environ={})


def test_whitespace_api_key_is_treated_as_missing() -> None:
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY is required"):
        ProofCoderConfig.from_env(environ={"DEEPSEEK_API_KEY": "   "})


def test_config_repr_never_contains_api_key() -> None:
    config = ProofCoderConfig.from_env(environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL})

    assert SENSITIVE_SENTINEL not in repr(config)
    assert "api_key" not in repr(config)
