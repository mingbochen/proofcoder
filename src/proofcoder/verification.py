"""Local modification and verification evidence tracking for Stage D1."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from proofcoder.state import CommandObservation, RunState
from proofcoder.tools.base import ToolResult

_MODIFICATION_TOOLS = frozenset({"create_file", "replace_in_file"})
_VERIFICATION_COMMAND_KINDS = frozenset({"test", "build", "static_check"})


class VerificationTracker:
    """Update an explicitly supplied ``RunState`` from local tool results."""

    def __init__(self, state: RunState) -> None:
        self._state = state

    @property
    def state(self) -> RunState:
        """Return the run state receiving locally observed evidence."""

        return self._state

    @property
    def valid_verification(self) -> CommandObservation | None:
        """Return the latest evidence that remains valid after all modifications."""

        return self._state.latest_verification

    def record_execution(self, tool_name: str, result: ToolResult) -> int:
        """Record one actually executed tool result and return its local event number."""

        event_sequence = self._state.next_event()
        if tool_name in _MODIFICATION_TOOLS:
            self._record_modification(result, event_sequence)
        elif tool_name == "run_command":
            self._record_command(result, event_sequence)
        return event_sequence

    def _record_modification(self, result: ToolResult, event_sequence: int) -> None:
        if not result.ok or result.data is None:
            return
        path_value = result.data.get("path")
        if not isinstance(path_value, str):
            return
        path = _normalized_local_result_path(path_value)
        if path is None:
            return
        self._state.record_changed_file(path, event_sequence)

    def _record_command(self, result: ToolResult, event_sequence: int) -> None:
        if result.data is None:
            return
        data = result.data
        argv_value = data.get("argv")
        cwd = data.get("cwd")
        exit_code = data.get("exit_code")
        timed_out = data.get("timed_out")
        command_kind = data.get("command_kind")
        if (
            not isinstance(argv_value, list)
            or not argv_value
            or not all(isinstance(item, str) for item in argv_value)
            or not isinstance(cwd, str)
            or (exit_code is not None and type(exit_code) is not int)
            or type(timed_out) is not bool
            or not isinstance(command_kind, str)
        ):
            return

        accepted = (
            result.ok
            and not timed_out
            and exit_code == 0
            and command_kind in _VERIFICATION_COMMAND_KINDS
        )
        self._state.record_command(
            CommandObservation(
                argv=tuple(argv_value),
                cwd=cwd,
                exit_code=exit_code,
                timed_out=timed_out,
                command_kind=command_kind,
                event_sequence=event_sequence,
                accepted_as_verification=accepted,
            )
        )


def _normalized_local_result_path(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or path == PurePosixPath(".")
        or ".." in path.parts
    ):
        return None
    return path.as_posix()
