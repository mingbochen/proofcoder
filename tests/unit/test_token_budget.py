"""Offline tests for the token budget layered over the byte budget.

The byte budget is the deterministic gate. A token budget may only tighten it, and the
ratio that converts tokens to bytes comes from the provider's own reported counts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofcoder.agent import AgentLoop
from proofcoder.context import (
    DEFAULT_TOKEN_SIZE_BYTES,
    MAX_TOKEN_SIZE_BYTES,
    MIN_TOKEN_SIZE_BYTES,
    TOKEN_CALIBRATION_MARGIN,
    ContextManager,
    TokenBudget,
)
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, TokenUsage, ToolCall
from proofcoder.tools.files import create_list_files_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry


def test_before_any_observation_the_default_ratio_is_used() -> None:
    budget = TokenBudget(1000)

    assert budget.bytes_per_token == DEFAULT_TOKEN_SIZE_BYTES
    assert budget.byte_limit() == int(1000 * DEFAULT_TOKEN_SIZE_BYTES)


def test_an_observation_recalibrates_with_a_safety_margin() -> None:
    budget = TokenBudget(1000)

    budget.observe(request_bytes=4000, prompt_tokens=1000)

    assert budget.bytes_per_token == pytest.approx(4.0 * TOKEN_CALIBRATION_MARGIN)
    assert budget.byte_limit() == int(1000 * 4.0 * TOKEN_CALIBRATION_MARGIN)


@pytest.mark.parametrize("prompt_tokens", [None, 0, -5])
def test_a_missing_or_nonsense_count_leaves_the_ratio_unchanged(prompt_tokens: int | None) -> None:
    budget = TokenBudget(1000)

    budget.observe(request_bytes=4000, prompt_tokens=prompt_tokens)

    assert budget.bytes_per_token == DEFAULT_TOKEN_SIZE_BYTES


def test_a_provider_that_under_reports_cannot_loosen_the_budget_without_limit() -> None:
    budget = TokenBudget(1000)

    budget.observe(request_bytes=1_000_000, prompt_tokens=1)

    assert budget.bytes_per_token == MAX_TOKEN_SIZE_BYTES


def test_a_provider_that_over_reports_cannot_shrink_the_budget_to_nothing() -> None:
    budget = TokenBudget(1000)

    budget.observe(request_bytes=1, prompt_tokens=1_000_000)

    assert budget.bytes_per_token == MIN_TOKEN_SIZE_BYTES


def test_a_non_positive_token_budget_is_refused() -> None:
    with pytest.raises(ValueError):
        TokenBudget(0)


def test_a_token_budget_only_ever_tightens_the_byte_budget() -> None:
    loose = ContextManager(budget_bytes=4096, token_budget=TokenBudget(1_000_000))
    tight = ContextManager(budget_bytes=1_000_000, token_budget=TokenBudget(1024))
    alone = ContextManager(budget_bytes=4096)

    assert loose.effective_budget_bytes() == 4096
    assert tight.effective_budget_bytes() == int(1024 * DEFAULT_TOKEN_SIZE_BYTES)
    assert alone.effective_budget_bytes() == 4096
    assert alone.token_budget is None


def test_observing_usage_without_a_token_budget_is_a_no_op() -> None:
    manager = ContextManager(budget_bytes=4096)

    manager.observe_usage(4000, 1000)

    assert manager.effective_budget_bytes() == 4096


def _response(call: ToolCall, prompt_tokens: int) -> ModelResponse:
    return ModelResponse(
        content=None,
        reasoning_content=None,
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=5, total_tokens=None),
        tool_calls=(call,),
    )


def test_the_loop_feeds_the_providers_count_back_into_the_next_request(tmp_path: Path) -> None:
    """One provider-measured request recalibrates the budget for the one after it."""

    (tmp_path / "visible.py").write_text("value = 1\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(create_list_files_tool(tmp_path))
    registry.register(create_finish_task_tool(tmp_path))
    client = ScriptedClient(
        [
            _response(
                ToolCall(id="c1", function=FunctionCall(name="list_files", arguments="{}")),
                prompt_tokens=100,
            ),
            _response(
                ToolCall(
                    id="c2",
                    function=FunctionCall(
                        name="finish_task", arguments=json.dumps({"summary": "done"})
                    ),
                ),
                prompt_tokens=200,
            ),
        ]
    )
    loop = AgentLoop(
        client=client,
        registry=registry,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=4,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        random_value=lambda: 0.0,
        context_budget_tokens=100_000,
    )

    loop.run("list")

    budget = loop._context.token_budget
    assert budget is not None
    assert budget.bytes_per_token != DEFAULT_TOKEN_SIZE_BYTES
    assert MIN_TOKEN_SIZE_BYTES <= budget.bytes_per_token <= MAX_TOKEN_SIZE_BYTES


def test_without_a_token_budget_the_request_is_unchanged(tmp_path: Path) -> None:
    """Default behavior must be byte-for-byte what it was before token budgets."""

    registry = ToolRegistry()
    registry.register(create_finish_task_tool(tmp_path))
    call = ToolCall(
        id="c1",
        function=FunctionCall(name="finish_task", arguments=json.dumps({"summary": "done"})),
    )
    with_none = ScriptedClient([_response(call, prompt_tokens=10)])
    AgentLoop(
        client=with_none,
        registry=registry,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=2,
        clock=lambda: 0.0,
    ).run("task")

    registry_again = ToolRegistry()
    registry_again.register(create_finish_task_tool(tmp_path))
    with_loose = ScriptedClient([_response(call, prompt_tokens=10)])
    AgentLoop(
        client=with_loose,
        registry=registry_again,
        workspace=tmp_path,
        system_prompt="system",
        max_steps=2,
        clock=lambda: 0.0,
        context_budget_tokens=1_000_000,
    ).run("task")

    assert with_none.requests[0].messages == with_loose.requests[0].messages
