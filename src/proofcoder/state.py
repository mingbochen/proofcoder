"""Program-owned run facts and evidence records through Stage D2."""

from __future__ import annotations

from dataclasses import dataclass, field

from proofcoder.protocol import CompletionStatus, TerminationReason


@dataclass(frozen=True, slots=True)
class CommandObservation:
    """One command that was actually started by ``run_command``."""

    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    timed_out: bool
    command_kind: str
    event_sequence: int
    accepted_as_verification: bool


@dataclass(slots=True)
class RunState:
    """Program-owned facts for one synchronous agent run."""

    original_task: str
    run_id: str = ""
    started_at: float = 0.0
    elapsed_seconds: float = 0.0
    final_text: str | None = None
    model_step: int = 0
    event_sequence: int = 0
    last_modified_events: dict[str, int] = field(default_factory=dict)
    command_observations: list[CommandObservation] = field(default_factory=list)
    latest_verification: CommandObservation | None = None
    model_call_count: int = 0
    tool_call_count: int = 0
    tool_error_count: int = 0
    api_attempt_count: int = 0
    api_retry_count: int = 0
    context_compaction_count: int = 0
    compressed_group_count: int = 0
    protocol_repair_count: int = 0
    consecutive_failure_count: int = 0
    no_progress_count: int = 0
    input_token_count: int = 0
    output_token_count: int = 0
    stable_error_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    termination_reason: TerminationReason | None = None
    completion_status: CompletionStatus | None = None
    limitations: tuple[str, ...] = ()
    blocked_reason: str | None = None
    finish_warnings: tuple[str, ...] = ()
    _changed_files: list[str] = field(default_factory=list, repr=False)

    @property
    def changed_files(self) -> tuple[str, ...]:
        """Return locally confirmed changed files in first-modification order."""

        return tuple(self._changed_files)

    @property
    def last_modification_event(self) -> int | None:
        """Return the most recent successful file-modification event."""

        if not self.last_modified_events:
            return None
        return max(self.last_modified_events.values())

    def next_event(self) -> int:
        """Allocate the next strictly increasing local event sequence."""

        self.event_sequence += 1
        return self.event_sequence

    def record_changed_file(self, path: str, event_sequence: int) -> None:
        """Record a successful local modification and invalidate older verification."""

        if path not in self.last_modified_events:
            self._changed_files.append(path)
        self.last_modified_events[path] = event_sequence
        self.latest_verification = None

    def record_command(self, observation: CommandObservation) -> None:
        """Record one actual command observation and any valid verification evidence."""

        self.command_observations.append(observation)
        if observation.accepted_as_verification:
            self.latest_verification = observation

    def record_error_code(self, code: str) -> None:
        """Retain a bounded list of stable error codes for local context facts."""

        self.stable_error_codes.append(code)
        del self.stable_error_codes[:-16]

    def warn(self, code: str) -> None:
        """Record one safe program-generated warning code."""

        self.warnings.append(code)
