"""Shared construction for one isolated local agent runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from proofcoder.agent import AgentLoop
from proofcoder.approval import ApprovalGate
from proofcoder.checkpoint import (
    DEFAULT_CHECKPOINT_LIMITS,
    CheckpointCapture,
    CheckpointError,
    CheckpointLimits,
    checkpoint_event_payload,
    create_checkpoint,
)
from proofcoder.context import DEFAULT_CONTEXT_BUDGET_BYTES, MessageHistory
from proofcoder.events import (
    CompositeSink,
    EventEmitter,
    EventSink,
    EventType,
    new_run_id,
)
from proofcoder.llm.base import LLMClient
from proofcoder.prompt import STAGE_B_SYSTEM_PROMPT
from proofcoder.protocol import CompletionStatus, RunResult, TerminationReason
from proofcoder.retry import DEFAULT_MAX_API_ATTEMPTS
from proofcoder.safety.policy import CommandPolicy
from proofcoder.session import SessionCarry
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import (
    create_create_file_tool,
    create_patch_file_tool,
    create_replace_in_file_tool,
)
from proofcoder.tools.files import create_list_files_tool, create_read_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.paths import (
    create_delete_path_tool,
    create_make_directory_tool,
    create_move_path_tool,
)
from proofcoder.tools.registry import ToolRegistry
from proofcoder.tools.search import create_search_text_tool
from proofcoder.trace import TraceRecorder


@dataclass(frozen=True, slots=True)
class AgentRunLimits:
    """Configuration shared by interactive and evaluation agent runs."""

    max_steps: int = 8
    max_seconds: float = 600.0
    context_budget_bytes: int = DEFAULT_CONTEXT_BUDGET_BYTES
    max_consecutive_failures: int = 5
    max_api_attempts: int = DEFAULT_MAX_API_ATTEMPTS


@dataclass(slots=True)
class AgentRuntimeResources:
    """Fresh registry and trace resources owned by exactly one agent run."""

    workspace: Path
    run_id: str
    registry: ToolRegistry
    recorder: TraceRecorder
    checkpoint: CheckpointCapture | None = None
    checkpoint_error: CheckpointError | None = None
    policy: CommandPolicy | None = None
    approval: ApprovalGate | None = None
    carry: SessionCarry | None = None

    def event_sink(self, additional_sinks: Sequence[EventSink] = ()) -> CompositeSink:
        """Combine optional presentation sinks with the mandatory local trace."""

        return CompositeSink(*additional_sinks, self.recorder)

    def close(self) -> None:
        """Close the owned trace recorder."""

        self.recorder.close()


def create_agent_runtime_resources(
    workspace: Path,
    *,
    environ: Mapping[str, str] | None = None,
    sensitive_values: tuple[str, ...] = (),
    run_id_factory: Callable[[], str] = new_run_id,
    checkpoint_enabled: bool = True,
    checkpoint_limits: CheckpointLimits = DEFAULT_CHECKPOINT_LIMITS,
    policy: CommandPolicy | None = None,
    approval: ApprovalGate | None = None,
    carry: SessionCarry | None = None,
) -> AgentRuntimeResources:
    """Create one fresh tool registry, trace recorder, and run checkpoint.

    The checkpoint is captured here, before any tool exists to be called, so the
    baseline predates every write the run can perform. A capture failure is returned
    on the resources rather than raised: the caller still owns the trace and ends the
    run through the same termination path as any other setup failure, because a run
    that starts without a checkpoint is exactly what specification section 10.5.1
    refuses.
    """

    workspace_root = workspace.resolve(strict=True)
    run_id = run_id_factory()
    recorder = TraceRecorder(
        workspace_root,
        run_id,
        sensitive_values=sensitive_values,
    )
    checkpoint: CheckpointCapture | None = None
    checkpoint_error: CheckpointError | None = None
    if checkpoint_enabled:
        try:
            checkpoint = create_checkpoint(workspace_root, run_id, limits=checkpoint_limits)
        except CheckpointError as error:
            checkpoint_error = error

    # The registry is built after the capture because the tools that destroy content
    # are only admissible when a baseline exists, and that is a fact of this run rather
    # than an argument the model can supply.
    available = checkpoint is not None
    # The policy arrives already frozen and the gate already configured. Neither is read
    # from disk here, so nothing the run writes can change what this registry decides.
    gate = ApprovalGate() if approval is None else approval
    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace_root))
    registry.register(create_search_text_tool(workspace_root))
    registry.register(create_read_file_tool(workspace_root))
    registry.register(create_create_file_tool(workspace_root, checkpoint_available=available))
    registry.register(create_replace_in_file_tool(workspace_root))
    registry.register(create_patch_file_tool(workspace_root))
    registry.register(create_make_directory_tool(workspace_root))
    registry.register(create_delete_path_tool(workspace_root, checkpoint_available=available))
    registry.register(create_move_path_tool(workspace_root, checkpoint_available=available))
    registry.register(
        create_run_command_tool(
            workspace_root,
            environ=environ,
            policy=policy,
            approval=gate,
        )
    )
    registry.register(create_finish_task_tool(workspace_root))
    return AgentRuntimeResources(
        workspace=workspace_root,
        run_id=run_id,
        registry=registry,
        recorder=recorder,
        checkpoint=checkpoint,
        checkpoint_error=checkpoint_error,
        policy=policy,
        approval=gate,
        carry=carry,
    )


def build_agent_loop(
    *,
    client: LLMClient,
    resources: AgentRuntimeResources,
    limits: AgentRunLimits,
    additional_sinks: Sequence[EventSink] = (),
    sensitive_values: tuple[str, ...] = (),
    cancel_requested: Callable[[], bool] | None = None,
) -> AgentLoop:
    """Build a new AgentLoop over resources that are not shared with another run."""

    return AgentLoop(
        client=client,
        registry=resources.registry,
        workspace=resources.workspace,
        system_prompt=STAGE_B_SYSTEM_PROMPT,
        max_steps=limits.max_steps,
        max_seconds=limits.max_seconds,
        context_budget_bytes=limits.context_budget_bytes,
        max_consecutive_failures=limits.max_consecutive_failures,
        max_api_attempts=limits.max_api_attempts,
        event_sink=resources.event_sink(additional_sinks),
        run_id_factory=lambda: resources.run_id,
        sensitive_values=sensitive_values,
        trace_path=resources.recorder.trace_path,
        cancel_requested=cancel_requested,
        checkpoint=resources.checkpoint,
        approval=resources.approval,
        policy=resources.policy,
        carry=resources.carry,
    )


def emit_setup_termination(
    *,
    task: str,
    resources: AgentRuntimeResources,
    termination_reason: TerminationReason,
    additional_sinks: Sequence[EventSink] = (),
    sensitive_values: tuple[str, ...] = (),
) -> None:
    """Persist a minimal complete trajectory when setup stops before AgentLoop."""

    emitter = EventEmitter(
        run_id=resources.run_id,
        sink=resources.event_sink(additional_sinks),
        sensitive_values=sensitive_values,
    )
    emitter.emit(EventType.TASK, step=0, payload={"task": task})
    if resources.checkpoint is not None:
        # A run that stops during setup still has a baseline on disk. Saying so keeps
        # the trace, not the filesystem, the place that answers whether one exists.
        emitter.emit(
            EventType.CHECKPOINT,
            step=0,
            payload=checkpoint_event_payload(resources.checkpoint),
        )
    emitter.emit(
        EventType.TERMINATION,
        step=0,
        payload={
            "api_attempts": 0,
            "api_retries": 0,
            "changed_files": [],
            "completion_status": "none",
            "context_compactions": 0,
            "elapsed_seconds": 0.0,
            "event_count": emitter.event_count + 1,
            "input_tokens": 0,
            "model_calls": 0,
            "output_tokens": 0,
            "termination_reason": termination_reason.value,
            "tool_calls": 0,
            "tool_errors": 0,
            "trace_complete": emitter.trace_complete,
            "trace_path": resources.recorder.trace_path,
            "verification": None,
            "warning_count": 0,
        },
    )


def setup_failure_result(
    *,
    task: str,
    resources: AgentRuntimeResources,
    termination_reason: TerminationReason,
) -> RunResult:
    """Build the local result paired with ``emit_setup_termination``."""

    history = MessageHistory()
    history.add_system(STAGE_B_SYSTEM_PROMPT)
    history.add_user(task)
    return RunResult(
        termination_reason=termination_reason,
        final_text=None,
        history=history,
        model_call_count=0,
        tool_call_count=0,
        tool_error_count=0,
        run_id=resources.run_id,
        trace_path=resources.recorder.trace_path,
        trace_complete=resources.recorder.trace_complete,
    )


def run_exit_code(
    termination_reason: TerminationReason,
    completion_status: CompletionStatus | None,
) -> int:
    """Map one terminated run onto the documented process exit code.

    Shared so the command line and the local web server report the same outcome for
    the same evidence rather than maintaining two drifting tables.
    """

    if termination_reason is TerminationReason.INTERRUPTED:
        return 130
    if termination_reason is not TerminationReason.FINISH_TASK:
        return 1
    return {
        CompletionStatus.COMPLETED_VERIFIED: 0,
        CompletionStatus.COMPLETED_NO_CHANGES: 0,
        CompletionStatus.COMPLETED_UNVERIFIED: 3,
        CompletionStatus.BLOCKED: 4,
        None: 1,
    }[completion_status]
