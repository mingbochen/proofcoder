"""Synchronous model-tool-message loop with Stage D2 bounded recovery."""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from proofcoder.context import (
    DEFAULT_CONTEXT_BUDGET_BYTES,
    ContextManager,
    MessageHistory,
)
from proofcoder.errors import ContextBudgetError, LLMRequestError, ProofCoderError
from proofcoder.llm.base import LLMClient
from proofcoder.progress import NoProgressTracker
from proofcoder.protocol import ModelResponse, RunResult, TerminationReason, ToolCall
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS, retry_delay_seconds
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
    modified_workspace: bool = False
    interrupted: bool = False


DEFAULT_MAX_SECONDS = 600.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
PROTOCOL_REPAIR_MESSAGE = (
    "PROGRAM_PROTOCOL_REPAIR: The previous response had no tool calls. "
    "Continue by calling one or more registered tools."
)
NO_PROGRESS_MESSAGE = (
    "PROGRAM_NO_PROGRESS: The last two completed tool batches had identical semantic "
    "actions and results. Choose a materially different next action."
)


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
        max_seconds: float = DEFAULT_MAX_SECONDS,
        context_budget_bytes: int = DEFAULT_CONTEXT_BUDGET_BYTES,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        max_api_attempts: int = DEFAULT_MAX_API_ATTEMPTS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        workspace_root = workspace.resolve(strict=True)
        if not workspace_root.is_dir():
            raise ValueError("workspace must be an existing directory")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be at least 1")
        if not 1 <= max_api_attempts <= DEFAULT_MAX_API_ATTEMPTS:
            raise ValueError("max_api_attempts must be between 1 and 3")
        self._client = client
        self._registry = registry
        self._workspace = workspace_root
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._max_seconds = max_seconds
        self._max_consecutive_failures = max_consecutive_failures
        self._max_api_attempts = max_api_attempts
        self._clock = clock
        self._sleep = sleep
        self._random_value = random_value
        self._context = ContextManager(budget_bytes=context_budget_bytes)

    @property
    def workspace(self) -> Path:
        """Return the resolved workspace used by this loop."""

        return self._workspace

    def run(self, task: str) -> RunResult:
        """Run until explicit completion or one controlled Stage D2 boundary."""

        history = MessageHistory()
        history.add_system(self._system_prompt)
        history.add_user(task)
        state = RunState(original_task=task, started_at=self._clock())
        tracker = VerificationTracker(state)
        try:
            return self._run(history, state, tracker)
        except KeyboardInterrupt:
            self._observe_time(state)
            state.termination_reason = TerminationReason.INTERRUPTED
            return self._result(state=state, history=history, final_text=state.final_text)
        except Exception:
            self._observe_time(state)
            state.record_error_code("INTERNAL_ERROR")
            state.termination_reason = TerminationReason.INTERNAL_ERROR
            return self._result(state=state, history=history, final_text=state.final_text)

    def _run(
        self,
        history: MessageHistory,
        state: RunState,
        tracker: VerificationTracker,
    ) -> RunResult:
        final_text: str | None = None
        progress = NoProgressTracker()
        schemas = self._registry.schemas()

        while state.model_call_count < self._max_steps:
            if self._time_exhausted(state):
                state.termination_reason = TerminationReason.MAX_TIME
                return self._result(state=state, history=history, final_text=final_text)
            try:
                view = self._context.build(history, schemas, state)
            except ContextBudgetError:
                state.record_error_code("CONTEXT_BUDGET_EXCEEDED")
                state.termination_reason = TerminationReason.CONTEXT_BUDGET_EXCEEDED
                return self._result(state=state, history=history, final_text=final_text)
            if view.compressed_group_count > state.compressed_group_count:
                state.compressed_group_count = view.compressed_group_count
                state.context_compaction_count += 1
                state.warn("CONTEXT_COMPACTED")

            response = self._request_model(view.messages, schemas, state)
            if response is None:
                return self._result(state=state, history=history, final_text=final_text)
            history.add_assistant(response)
            state.model_call_count += 1
            state.model_step = state.model_call_count
            if response.content:
                final_text = response.content
                state.final_text = response.content

            if not response.tool_calls:
                if state.protocol_repair_count == 0:
                    state.protocol_repair_count = 1
                    state.warn("PROTOCOL_REPAIR")
                    history.add_user(PROTOCOL_REPAIR_MESSAGE)
                    continue
                state.termination_reason = TerminationReason.MODEL_STOPPED
                return self._result(state=state, history=history, final_text=final_text)

            if self._time_exhausted(state):
                state.tool_call_count += len(response.tool_calls)
                for call in response.tool_calls:
                    result = _not_started_result()
                    state.tool_error_count += 1
                    state.record_error_code("BATCH_NOT_STARTED")
                    history.add_tool(call.id, result.to_json())
                state.termination_reason = TerminationReason.MAX_TIME
                return self._result(state=state, history=history, final_text=final_text)

            state.tool_call_count += len(response.tool_calls)
            batch = self._execute_batch(response.tool_calls, tracker)
            for call, result in zip(response.tool_calls, batch.results, strict=True):
                if not result.ok:
                    state.tool_error_count += 1
                    if result.error is not None:
                        state.record_error_code(result.error.code)
                history.add_tool(call.id, result.to_json())

            if batch.interrupted:
                self._observe_time(state)
                state.termination_reason = TerminationReason.INTERRUPTED
                return self._result(state=state, history=history, final_text=final_text)
            if self._time_exhausted(state):
                state.termination_reason = TerminationReason.MAX_TIME
                return self._result(state=state, history=history, final_text=final_text)
            if batch.finish is not None:
                state.termination_reason = TerminationReason.FINISH_TASK
                state.completion_status = batch.finish.status
                return self._result(
                    state=state,
                    history=history,
                    final_text=final_text,
                    final_report=batch.finish.final_report,
                )

            self._update_consecutive_failures(response.tool_calls, batch, state)
            if state.consecutive_failure_count >= self._max_consecutive_failures:
                state.termination_reason = TerminationReason.MAX_CONSECUTIVE_FAILURES
                return self._result(state=state, history=history, final_text=final_text)

            observation = progress.observe(
                response.tool_calls,
                batch.results,
                modified_workspace=batch.modified_workspace,
            )
            state.no_progress_count = observation.repeat_count
            if observation.terminate:
                state.termination_reason = TerminationReason.NO_PROGRESS
                return self._result(state=state, history=history, final_text=final_text)
            if observation.warn:
                state.warn("NO_PROGRESS")
                history.add_user(NO_PROGRESS_MESSAGE)

        self._observe_time(state)
        state.termination_reason = TerminationReason.MAX_STEPS
        return self._result(state=state, history=history, final_text=final_text)

    def _request_model(
        self,
        messages: tuple[dict[str, object], ...],
        schemas: list[dict[str, object]],
        state: RunState,
    ) -> ModelResponse | None:
        attempts = 0
        while attempts < self._max_api_attempts:
            if self._time_exhausted(state):
                state.termination_reason = TerminationReason.MAX_TIME
                return None
            attempts += 1
            state.api_attempt_count += 1
            try:
                return self._client.complete(messages, schemas)
            except LLMRequestError as error:
                state.record_error_code(f"LLM_{error.category.value.upper()}")
                if not error.retryable or attempts >= self._max_api_attempts:
                    state.termination_reason = TerminationReason.API_ERROR
                    return None
                delay = retry_delay_seconds(
                    error,
                    retry_number=attempts,
                    random_value=self._random_value(),
                )
                remaining = self._remaining_seconds(state)
                if delay >= remaining:
                    state.termination_reason = TerminationReason.MAX_TIME
                    return None
                state.api_retry_count += 1
                state.warn("API_RETRY")
                self._sleep(delay)
            except ProofCoderError:
                state.record_error_code("LLM_PERMANENT")
                state.termination_reason = TerminationReason.API_ERROR
                return None
        state.termination_reason = TerminationReason.API_ERROR
        return None

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
        try:
            prepared = [self._registry.prepare(call) for call in calls]
        except KeyboardInterrupt:
            return _BatchOutcome(
                results=tuple(_interrupted_result(False) for _ in calls),
                interrupted=True,
            )
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
        modified_workspace = False
        for index, item in enumerate(prepared):
            if not isinstance(item, PreparedToolCall):
                continue
            try:
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
                modified_workspace = modified_workspace or (
                    item.definition.modifies_workspace and result.ok
                )
                tracker.record_execution(item.definition.name, result)
                results.append(result)
            except KeyboardInterrupt:
                results.append(_interrupted_result(True))
                results.extend(_interrupted_result(False) for _ in prepared[index + 1 :])
                return _BatchOutcome(
                    results=tuple(results),
                    modified_workspace=modified_workspace,
                    interrupted=True,
                )
        return _BatchOutcome(
            results=tuple(results),
            finish=finish,
            modified_workspace=modified_workspace,
        )

    @staticmethod
    def _update_consecutive_failures(
        calls: tuple[ToolCall, ...],
        batch: _BatchOutcome,
        state: RunState,
    ) -> None:
        if batch.modified_workspace:
            state.consecutive_failure_count = 0
            return
        if batch.results and all(not result.ok for result in batch.results):
            state.consecutive_failure_count += 1
            return
        ordinary_success = any(
            call.function.name != FINISH_TASK_NAME and result.ok
            for call, result in zip(calls, batch.results, strict=True)
        )
        if ordinary_success:
            state.consecutive_failure_count = 0

    def _remaining_seconds(self, state: RunState) -> float:
        self._observe_time(state)
        return max(0.0, self._max_seconds - state.elapsed_seconds)

    def _time_exhausted(self, state: RunState) -> bool:
        return self._remaining_seconds(state) <= 0

    def _observe_time(self, state: RunState) -> None:
        state.elapsed_seconds = max(0.0, self._clock() - state.started_at)

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
            elapsed_seconds=state.elapsed_seconds,
            api_attempt_count=state.api_attempt_count,
            api_retry_count=state.api_retry_count,
            context_compaction_count=state.context_compaction_count,
            consecutive_failure_count=state.consecutive_failure_count,
            no_progress_count=state.no_progress_count,
            warnings=tuple(state.warnings),
        )


def _interrupted_result(execution_started: bool) -> ToolResult:
    code = "TOOL_INTERRUPTED" if execution_started else "BATCH_INTERRUPTED"
    message = (
        "tool execution was interrupted"
        if execution_started
        else "tool was not executed because the batch was interrupted"
    )
    return ToolResult.failure(
        code,
        message,
        retryable=False,
        data={"execution_started": execution_started},
    )


def _not_started_result() -> ToolResult:
    return ToolResult.failure(
        "BATCH_NOT_STARTED",
        "tool was not executed because the run time limit was reached",
        retryable=False,
        data={"execution_started": False},
    )
