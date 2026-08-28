"""Synchronous model-tool-message loop with Stage D1 completion evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from proofcoder.context import MessageHistory
from proofcoder.errors import ProofCoderError
from proofcoder.llm.base import LLMClient
from proofcoder.protocol import RunResult, TerminationReason, ToolCall
from proofcoder.state import RunState
from proofcoder.tools.base import PreparedToolCall, ToolResult
from proofcoder.tools.finish import (
    FINISH_TASK_NAME,
    FinishOutcome,
    FinishTaskRequest,
    build_finish_outcome,
    parse_finish_task_request,
)
from proofcoder.tools.registry import ToolRegistry
from proofcoder.verification import VerificationTracker


@dataclass(frozen=True, slots=True)
class _BatchOutcome:
    results: tuple[ToolResult, ...]
    finish: FinishOutcome | None = None


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
        """Run until explicit completion, model stop, budget exhaustion, or API failure."""

        history = MessageHistory()
        history.add_system(self._system_prompt)
        history.add_user(task)
        state = RunState(original_task=task)
        tracker = VerificationTracker(state)
        final_text: str | None = None

        for step in range(1, self._max_steps + 1):
            state.model_step = step
            state.model_call_count += 1
            try:
                response = self._client.complete(
                    history.to_api_messages(),
                    self._registry.schemas(),
                )
                history.add_assistant(response)
            except ProofCoderError:
                state.termination_reason = TerminationReason.API_ERROR
                return self._result(
                    state=state,
                    history=history,
                    final_text=final_text,
                )

            if response.content:
                final_text = response.content
            if not response.tool_calls:
                state.termination_reason = TerminationReason.MODEL_STOPPED
                return self._result(
                    state=state,
                    history=history,
                    final_text=final_text,
                )

            state.tool_call_count += len(response.tool_calls)
            batch = self._execute_batch(response.tool_calls, tracker)
            for call, result in zip(response.tool_calls, batch.results, strict=True):
                if not result.ok:
                    state.tool_error_count += 1
                history.add_tool(call.id, result.to_json())
            if batch.finish is not None:
                state.termination_reason = TerminationReason.FINISH_TASK
                state.completion_status = batch.finish.status
                return self._result(
                    state=state,
                    history=history,
                    final_text=final_text,
                    final_report=batch.finish.final_report,
                )

        state.termination_reason = TerminationReason.MAX_STEPS
        return self._result(
            state=state,
            history=history,
            final_text=final_text,
        )

    def _execute_batch(
        self,
        calls: tuple[ToolCall, ...],
        tracker: VerificationTracker,
    ) -> _BatchOutcome:
        if len(calls) != 1 and any(call.function.name == FINISH_TASK_NAME for call in calls):
            return _BatchOutcome(
                results=tuple(
                    ToolResult.failure(
                        "FINISH_TASK_MUST_BE_SOLE_CALL",
                        "finish_task must be the only tool call in its assistant response; "
                        "no calls were executed",
                        retryable=True,
                    )
                    if call.function.name == FINISH_TASK_NAME
                    else ToolResult.failure(
                        "BATCH_REJECTED",
                        "finish_task appeared with another call; no calls were executed",
                        retryable=True,
                    )
                    for call in calls
                )
            )

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
            return _BatchOutcome(results=tuple(results))

        results: list[ToolResult] = []
        finish: FinishOutcome | None = None
        for item in prepared:
            if not isinstance(item, PreparedToolCall):
                continue
            if item.definition.name == FINISH_TASK_NAME:
                tracker.state.next_event()
                request = parse_finish_task_request(self._workspace, item.arguments)
                if isinstance(request, FinishTaskRequest):
                    finish = build_finish_outcome(request, tracker)
                    results.append(finish.result)
                else:
                    results.append(request)
                continue
            result = self._registry.execute(item)
            tracker.record_execution(item.definition.name, result)
            results.append(result)
        return _BatchOutcome(results=tuple(results), finish=finish)

    def _result(
        self,
        *,
        state: RunState,
        history: MessageHistory,
        final_text: str | None,
        final_report: str | None = None,
    ) -> RunResult:
        termination_reason = state.termination_reason
        if termination_reason is None:
            raise RuntimeError("run state must have a termination reason")
        verification = state.latest_verification
        return RunResult(
            termination_reason=termination_reason,
            final_text=final_text,
            history=history,
            model_call_count=state.model_call_count,
            tool_call_count=state.tool_call_count,
            tool_error_count=state.tool_error_count,
            completion_status=state.completion_status,
            final_report=final_report,
            changed_files=state.changed_files,
            verification_command=None if verification is None else verification.argv,
            verification_cwd=None if verification is None else verification.cwd,
            verification_exit_code=None if verification is None else verification.exit_code,
        )
