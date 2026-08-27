"""Synchronous Stage B model-tool-message loop."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from proofcoder.context import MessageHistory
from proofcoder.errors import ProofCoderError
from proofcoder.llm.base import LLMClient
from proofcoder.protocol import RunResult, TerminationReason, ToolCall
from proofcoder.tools.base import PreparedToolCall, ToolResult
from proofcoder.tools.registry import ToolRegistry


class AgentLoop:
    """Drive a bounded synchronous loop over one local tool registry."""

    def __init__(
        self,
        *,
        client: LLMClient,
        registry: ToolRegistry,
        workspace: Path,
        system_prompt: str,
        max_steps: int,
    ) -> None:
        workspace_root = workspace.resolve(strict=True)
        if not workspace_root.is_dir():
            raise ValueError("workspace must be an existing directory")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._client = client
        self._registry = registry
        self._workspace = workspace_root
        self._system_prompt = system_prompt
        self._max_steps = max_steps

    @property
    def workspace(self) -> Path:
        """Return the resolved workspace used by this loop."""

        return self._workspace

    def run(self, task: str) -> RunResult:
        """Run until the model stops, the call budget ends, or the client fails."""

        history = MessageHistory()
        history.add_system(self._system_prompt)
        history.add_user(task)
        model_call_count = 0
        tool_call_count = 0
        tool_error_count = 0
        final_text: str | None = None

        for _step in range(self._max_steps):
            model_call_count += 1
            try:
                response = self._client.complete(
                    history.to_api_messages(),
                    self._registry.schemas(),
                )
                history.add_assistant(response)
            except ProofCoderError:
                return RunResult(
                    termination_reason=TerminationReason.API_ERROR,
                    final_text=final_text,
                    history=history,
                    model_call_count=model_call_count,
                    tool_call_count=tool_call_count,
                    tool_error_count=tool_error_count,
                )

            if response.content:
                final_text = response.content
            if not response.tool_calls:
                return RunResult(
                    termination_reason=TerminationReason.MODEL_STOPPED,
                    final_text=final_text,
                    history=history,
                    model_call_count=model_call_count,
                    tool_call_count=tool_call_count,
                    tool_error_count=tool_error_count,
                )

            tool_call_count += len(response.tool_calls)
            results = self._execute_batch(response.tool_calls)
            for call, result in zip(response.tool_calls, results, strict=True):
                if not result.ok:
                    tool_error_count += 1
                history.add_tool(call.id, result.to_json())

        return RunResult(
            termination_reason=TerminationReason.MAX_STEPS,
            final_text=final_text,
            history=history,
            model_call_count=model_call_count,
            tool_call_count=tool_call_count,
            tool_error_count=tool_error_count,
        )

    def _execute_batch(self, calls: tuple[ToolCall, ...]) -> list[ToolResult]:
        duplicate_ids = {
            call_id for call_id, count in Counter(call.id for call in calls).items() if count > 1
        }
        prepared = [self._registry.prepare(call) for call in calls]
        batch_invalid = bool(duplicate_ids) or any(
            isinstance(item, ToolResult) for item in prepared
        )

        if batch_invalid:
            results: list[ToolResult] = []
            for call, item in zip(calls, prepared, strict=True):
                if call.id in duplicate_ids:
                    results.append(
                        ToolResult.failure(
                            "DUPLICATE_TOOL_CALL_ID",
                            "tool call IDs must be unique within one assistant response",
                            retryable=True,
                        )
                    )
                elif isinstance(item, ToolResult):
                    results.append(item)
                else:
                    results.append(
                        ToolResult.failure(
                            "BATCH_REJECTED",
                            "another call in this batch is invalid; no calls were executed",
                            retryable=True,
                        )
                    )
            return results

        return [
            self._registry.execute(item) for item in prepared if isinstance(item, PreparedToolCall)
        ]
