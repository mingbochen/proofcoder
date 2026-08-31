"""Synchronous model-tool-message loop with Stage D2 bounded recovery."""

from __future__ import annotations

import random
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from proofcoder.context import (
    DEFAULT_CONTEXT_BUDGET_BYTES,
    ContextManager,
    MessageHistory,
)
from proofcoder.errors import ContextBudgetError, LLMRequestError, ProofCoderError
from proofcoder.events import (
    EventEmitter,
    EventSink,
    EventType,
    NoOpSink,
    bound_text,
    diff_event_payload,
    new_run_id,
    summarize_tool_arguments,
    summarize_tool_result,
    verification_event_payload,
)
from proofcoder.llm.base import LLMClient
from proofcoder.progress import NoProgressTracker
from proofcoder.protocol import ModelResponse, RunResult, TerminationReason, ToolCall
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS, retry_delay_seconds
from proofcoder.safety.secrets import redact_text
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
OUTPUT_TRUNCATED_MESSAGE = (
    "PROGRAM_OUTPUT_TRUNCATED: The previous response stopped at the model output token "
    "limit, so any tool arguments it carried may be incomplete. Do not repeat the same "
    "oversized call. Create a large file as a short skeleton with create_file, then add "
    "the remaining sections with successive replace_in_file calls."
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
        event_sink: EventSink | None = None,
        run_id_factory: Callable[[], str] = new_run_id,
        event_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sensitive_values: tuple[str, ...] = (),
        trace_path: str | None = None,
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
        self._event_sink = NoOpSink() if event_sink is None else event_sink
        self._run_id_factory = run_id_factory
        self._event_clock = event_clock
        self._sensitive_values = sensitive_values
        self._trace_path = trace_path
        self._events: EventEmitter | None = None

    @property
    def workspace(self) -> Path:
        """Return the resolved workspace used by this loop."""

        return self._workspace

    def run(self, task: str) -> RunResult:
        """Run until explicit completion or one controlled Stage D2 boundary."""

        history = MessageHistory()
        history.add_system(self._system_prompt)
        history.add_user(task)
        run_id = self._run_id_factory()
        self._events = EventEmitter(
            run_id=run_id,
            sink=self._event_sink,
            clock=self._event_clock,
            sensitive_values=self._sensitive_values,
        )
        state = RunState(original_task=task, run_id=run_id, started_at=self._clock())
        tracker = VerificationTracker(state)
        self._emit(EventType.TASK, state, {"task": task})
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
                self._emit(
                    EventType.WARNING,
                    state,
                    {
                        "code": "CONTEXT_COMPACTED",
                        "compressed_groups": view.compressed_group_count,
                    },
                )

            response = self._request_model(view.messages, schemas, state)
            if response is None:
                return self._result(state=state, history=history, final_text=final_text)
            history.add_assistant(response)
            state.model_call_count += 1
            state.model_step = state.model_call_count
            if response.usage is not None:
                state.input_token_count += response.usage.prompt_tokens or 0
                state.output_token_count += response.usage.completion_tokens or 0
            if response.content:
                final_text = response.content
                state.final_text = response.content
            self._emit(
                EventType.MODEL,
                state,
                {
                    "api_attempts": state.api_attempt_count,
                    "finish_reason": response.finish_reason,
                    "input_tokens": (
                        None if response.usage is None else response.usage.prompt_tokens
                    ),
                    "output_tokens": (
                        None if response.usage is None else response.usage.completion_tokens
                    ),
                    "text": response.content,
                    "tool_call_count": len(response.tool_calls),
                },
            )
            for call in response.tool_calls:
                self._emit(
                    EventType.TOOL_CALL,
                    state,
                    {
                        "arguments": summarize_tool_arguments(
                            call.function.name,
                            call.function.arguments,
                            sensitive_values=self._sensitive_values,
                        ),
                        "tool_call_id": call.id,
                        "tool_name": call.function.name,
                    },
                )

            # A truncated response cannot be diagnosed from its parse errors alone: the
            # model would only observe INVALID_JSON on arguments it believes it completed.
            truncated_output = response.finish_reason == "length"
            if truncated_output:
                state.warn("OUTPUT_TRUNCATED")
                self._emit(
                    EventType.WARNING,
                    state,
                    {
                        "code": "OUTPUT_TRUNCATED",
                        "tool_call_count": len(response.tool_calls),
                    },
                )
            if not response.tool_calls:
                if state.protocol_repair_count == 0:
                    state.protocol_repair_count = 1
                    state.warn("PROTOCOL_REPAIR")
                    self._emit(
                        EventType.WARNING,
                        state,
                        {"code": "PROTOCOL_REPAIR", "repair_count": 1},
                    )
                    # Truncation already explains the missing tool calls, so it replaces
                    # the generic repair prompt: consecutive user messages are rejected.
                    history.add_user(
                        OUTPUT_TRUNCATED_MESSAGE if truncated_output else PROTOCOL_REPAIR_MESSAGE
                    )
                    continue
                state.termination_reason = TerminationReason.MODEL_STOPPED
                return self._result(state=state, history=history, final_text=final_text)

            if self._time_exhausted(state):
                state.tool_call_count += len(response.tool_calls)
                for call in response.tool_calls:
                    result = _not_started_result()
                    self._record_result(
                        call,
                        result,
                        history=history,
                        state=state,
                    )
                state.termination_reason = TerminationReason.MAX_TIME
                return self._result(state=state, history=history, final_text=final_text)

            state.tool_call_count += len(response.tool_calls)
            batch = self._execute_batch(
                response.tool_calls,
                tracker,
                on_result=lambda call, result: self._record_result(
                    call,
                    result,
                    history=history,
                    state=state,
                ),
            )

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
                finish_data = batch.finish.result.data or {}
                limitations = finish_data.get("limitations")
                if isinstance(limitations, list) and all(
                    isinstance(item, str) for item in limitations
                ):
                    state.limitations = tuple(limitations)
                blocked_reason = finish_data.get("blocked_reason")
                if isinstance(blocked_reason, str):
                    state.blocked_reason = blocked_reason
                state.finish_warnings = batch.finish.result.meta.warnings
                verification = state.latest_verification
                self._emit(
                    EventType.COMPLETION,
                    state,
                    {
                        "changed_files": list(state.changed_files),
                        "completion_status": batch.finish.status.value,
                        "verification": (
                            None
                            if verification is None
                            else {
                                "argv": list(verification.argv),
                                "cwd": verification.cwd,
                                "exit_code": verification.exit_code,
                            }
                        ),
                    },
                )
                return self._result(
                    state=state,
                    history=history,
                    final_text=final_text,
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
            # Deferred until every tool_call_id has its matching result, so the
            # assistant/tool group stays atomic, and combined into one user message
            # because the history rejects consecutive user turns.
            notes: list[str] = []
            if observation.warn:
                state.warn("NO_PROGRESS")
                self._emit(
                    EventType.WARNING,
                    state,
                    {"code": "NO_PROGRESS", "repeat_count": observation.repeat_count},
                )
                notes.append(NO_PROGRESS_MESSAGE)
            if truncated_output:
                notes.append(OUTPUT_TRUNCATED_MESSAGE)
            if notes:
                history.add_user("\n\n".join(notes))

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
                self._emit(
                    EventType.WARNING,
                    state,
                    {
                        "api_attempt": attempts,
                        "category": error.category.value,
                        "code": "API_RETRY",
                        "delay_seconds": delay,
                        "status_code": error.status_code,
                    },
                )
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
        *,
        on_result: Callable[[ToolCall, ToolResult], None],
    ) -> _BatchOutcome:
        if len(calls) != 1 and any(call.function.name == FINISH_TASK_NAME for call in calls):
            results = tuple(
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
            for call, result in zip(calls, results, strict=True):
                on_result(call, result)
            return _BatchOutcome(results=results)

        duplicate_ids = {
            call_id for call_id, count in Counter(call.id for call in calls).items() if count > 1
        }
        try:
            prepared = [self._registry.prepare(call) for call in calls]
        except KeyboardInterrupt:
            results = tuple(_interrupted_result(False) for _ in calls)
            for call, result in zip(calls, results, strict=True):
                on_result(call, result)
            return _BatchOutcome(results=results, interrupted=True)
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
            outcome_results = tuple(results)
            for call, result in zip(calls, outcome_results, strict=True):
                on_result(call, result)
            return _BatchOutcome(results=outcome_results)

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
                        on_result(item.call, finish.result)
                    else:
                        results.append(request)
                        on_result(item.call, request)
                    continue
                result = self._registry.execute(item)
                modified_workspace = modified_workspace or (
                    item.definition.modifies_workspace and result.ok
                )
                tracker.record_execution(item.definition.name, result)
                results.append(result)
                on_result(item.call, result)
            except KeyboardInterrupt:
                interrupted_result = _interrupted_result(True)
                results.append(interrupted_result)
                on_result(item.call, interrupted_result)
                for remaining in prepared[index + 1 :]:
                    if not isinstance(remaining, PreparedToolCall):
                        continue
                    skipped_result = _interrupted_result(False)
                    results.append(skipped_result)
                    on_result(remaining.call, skipped_result)
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

    def _record_result(
        self,
        call: ToolCall,
        result: ToolResult,
        *,
        history: MessageHistory,
        state: RunState,
    ) -> None:
        """Record one completed tool result and emit its local evidence immediately."""

        if not result.ok:
            state.tool_error_count += 1
            if result.error is not None:
                state.record_error_code(result.error.code)
        history.add_tool(call.id, result.to_json())
        self._emit(
            EventType.TOOL_RESULT,
            state,
            summarize_tool_result(
                call.function.name,
                call.id,
                result,
                sensitive_values=self._sensitive_values,
            ),
        )

        diff_payload = diff_event_payload(
            call.function.name,
            call.id,
            result,
            sensitive_values=self._sensitive_values,
        )
        if diff_payload is not None:
            self._emit(EventType.DIFF, state, diff_payload)

        if call.function.name == "run_command":
            data = result.data or {}
            accepted = (
                result.ok
                and data.get("timed_out") is False
                and data.get("exit_code") == 0
                and data.get("command_kind") in {"test", "build", "static_check"}
            )
            verification_payload = verification_event_payload(
                call.id,
                result,
                accepted=accepted,
                sensitive_values=self._sensitive_values,
            )
            if verification_payload is not None:
                self._emit(EventType.VERIFICATION, state, verification_payload)

        if result.error is not None:
            self._emit(
                EventType.WARNING,
                state,
                {
                    "code": result.error.code,
                    "message": result.error.message,
                    "tool_call_id": call.id,
                },
            )
        if result.meta.truncated:
            self._emit(
                EventType.WARNING,
                state,
                {
                    "code": "TOOL_RESULT_TRUNCATED",
                    "tool_call_id": call.id,
                    "tool_name": call.function.name,
                },
            )
        for warning in result.meta.warnings:
            code = warning.partition(":")[0] or "TOOL_WARNING"
            self._emit(
                EventType.WARNING,
                state,
                {"code": code, "message": warning, "tool_call_id": call.id},
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

    def _emit(
        self,
        event_type: EventType,
        state: RunState,
        payload: dict[str, object],
    ) -> None:
        events = self._events
        if events is None:
            raise RuntimeError("event emitter must be initialized before running")
        events.emit(event_type, step=state.model_step, payload=payload)

    def _result(
        self,
        *,
        state: RunState,
        history: MessageHistory,
        final_text: str | None,
    ) -> RunResult:
        termination_reason = state.termination_reason
        if termination_reason is None:
            raise RuntimeError("run state must have a termination reason")
        self._observe_time(state)
        events = self._events
        if events is None:
            raise RuntimeError("event emitter must be initialized before finalizing")
        for code in events.sink_error_codes:
            if code not in state.warnings:
                state.warn(code)
        verification = state.latest_verification
        verification_payload = (
            None
            if verification is None
            else {
                "argv": list(verification.argv),
                "cwd": verification.cwd,
                "exit_code": verification.exit_code,
            }
        )
        completion_status = (
            "none" if state.completion_status is None else state.completion_status.value
        )
        self._emit(
            EventType.TERMINATION,
            state,
            {
                "api_attempts": state.api_attempt_count,
                "api_retries": state.api_retry_count,
                "changed_files": list(state.changed_files),
                "completion_status": completion_status,
                "context_compactions": state.context_compaction_count,
                "elapsed_seconds": state.elapsed_seconds,
                "event_count": events.event_count + 1,
                "input_tokens": state.input_token_count,
                "model_calls": state.model_call_count,
                "no_progress_count": state.no_progress_count,
                "output_tokens": state.output_token_count,
                "termination_reason": termination_reason.value,
                "tool_calls": state.tool_call_count,
                "tool_errors": state.tool_error_count,
                "trace_complete": events.trace_complete,
                "trace_path": self._trace_path,
                "verification": verification_payload,
                "warning_count": events.warning_count,
            },
        )
        for code in events.sink_error_codes:
            if code not in state.warnings:
                state.warn(code)
        report = self._build_run_report(state)
        return RunResult(
            termination_reason=termination_reason,
            final_text=final_text,
            history=history,
            model_call_count=state.model_call_count,
            tool_call_count=state.tool_call_count,
            tool_error_count=state.tool_error_count,
            completion_status=state.completion_status,
            final_report=report,
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
            input_token_count=state.input_token_count,
            output_token_count=state.output_token_count,
            run_id=state.run_id,
            trace_path=self._trace_path,
            trace_complete=events.trace_complete,
            event_count=events.event_count,
        )

    def _build_run_report(
        self,
        state: RunState,
    ) -> str:
        """Build the final report from program-owned state and local observations."""

        status = (
            state.termination_reason.value
            if state.completion_status is None
            else state.completion_status.value
        )
        lines = [
            f"Completion status: {status}",
            "Changed files (local evidence):",
            *(f"- {path}" for path in state.changed_files),
        ]
        if not state.changed_files:
            lines.append("- none")
        lines.append("Command observations (local evidence):")
        for observation in state.command_observations:
            lines.append(
                f"- event={observation.event_sequence} cwd={observation.cwd} "
                f"argv={list(observation.argv)} exit_code={observation.exit_code} "
                f"timed_out={str(observation.timed_out).lower()} "
                f"kind={observation.command_kind} "
                f"verification={str(observation.accepted_as_verification).lower()}"
            )
        if not state.command_observations:
            lines.append("- none")
        verification = state.latest_verification
        if verification is None:
            lines.append("Valid verification after latest modification: none")
        else:
            lines.append(
                "Valid verification after latest modification: "
                f"event={verification.event_sequence} exit_code={verification.exit_code}"
            )
        lines.append("Limitations:")
        lines.extend(f"- {limitation}" for limitation in state.limitations)
        if not state.limitations:
            lines.append("- none")
        if state.blocked_reason is not None:
            lines.append(f"Blocked reason (model explanation): {state.blocked_reason}")
        if state.finish_warnings:
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in state.finish_warnings)

        lines.extend(
            [
                "Runtime statistics (local evidence):",
                f"- termination_reason={state.termination_reason.value}",
                f"- model_steps={state.model_call_count}",
                f"- api_attempts={state.api_attempt_count}",
                f"- api_retries={state.api_retry_count}",
                f"- tool_calls={state.tool_call_count}",
                f"- tool_errors={state.tool_error_count}",
                f"- context_compactions={state.context_compaction_count}",
                f"- warnings={self._events.warning_count}",
                f"- input_tokens={state.input_token_count}",
                f"- output_tokens={state.output_token_count}",
                f"- elapsed_seconds={state.elapsed_seconds:.3f}",
                f"- run_id={state.run_id}",
                f"- trace_path={self._trace_path}",
                f"- trace_complete={str(self._events.trace_complete).lower()}",
                f"- event_count={self._events.event_count}",
            ]
        )
        if state.termination_reason is not TerminationReason.FINISH_TASK:
            lines.append("Limitation: run ended before an accepted finish_task call")
        if not self._events.trace_complete:
            lines.append("Limitation: persisted trace is incomplete")
        report = redact_text("\n".join(lines), sensitive_values=self._sensitive_values)
        return bound_text(report, 32 * 1024)[0]


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
