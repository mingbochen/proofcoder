"""Stage F wiring: every run captures a baseline before its first model call."""

from __future__ import annotations

from pathlib import Path

import pytest

import proofcoder.agent_runtime as agent_runtime
from proofcoder.agent_runtime import create_agent_runtime_resources
from proofcoder.checkpoint import (
    CHECKPOINT_RELATIVE_ROOT,
    CheckpointError,
    CheckpointLimits,
    apply_rollback,
    load_checkpoint,
    plan_rollback,
    tool_written_paths,
)
from proofcoder.events import EventType
from proofcoder.protocol import TerminationReason
from proofcoder.trace import read_trace

RUN_ID = "d" * 32
# Stands in for a configured credential so the tests can prove it never reaches output.
SENSITIVE_SENTINEL = "never-print-this-value"


def _resources(workspace: Path, **kwargs: object) -> agent_runtime.AgentRuntimeResources:
    return create_agent_runtime_resources(
        workspace,
        run_id_factory=lambda: RUN_ID,
        **kwargs,  # type: ignore[arg-type]
    )


def test_runtime_captures_a_baseline_before_any_tool_exists(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    resources = _resources(tmp_path)
    try:
        assert resources.checkpoint is not None
        assert resources.checkpoint_error is None
        assert resources.checkpoint.run_id == RUN_ID
        assert resources.registry.find("replace_in_file") is not None
    finally:
        resources.close()

    entry = next(
        item for item in load_checkpoint(tmp_path, RUN_ID).entries if item.path == "app.py"
    )
    assert entry.restorable


def test_checkpoint_can_be_disabled_explicitly(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    resources = _resources(tmp_path, checkpoint_enabled=False)
    try:
        assert resources.checkpoint is None
        assert resources.checkpoint_error is None
    finally:
        resources.close()

    assert not (tmp_path / CHECKPOINT_RELATIVE_ROOT / RUN_ID).exists()


def test_capture_failure_is_reported_rather_than_raised(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"file{index}.txt").write_text("data\n", encoding="utf-8")

    resources = _resources(tmp_path, checkpoint_limits=CheckpointLimits(max_entries=1))
    try:
        assert resources.checkpoint is None
        assert isinstance(resources.checkpoint_error, CheckpointError)
        assert resources.checkpoint_error.code == "CHECKPOINT_LIMIT_EXCEEDED"
        # The trace still exists, so the caller can end the run through it.
        assert resources.recorder.trace_path
    finally:
        resources.close()


def test_run_command_reports_a_checkpoint_failure_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from rich.console import Console

    from proofcoder.cli import main

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    def failing_capture(*args: object, **kwargs: object) -> object:
        raise CheckpointError("CHECKPOINT_WRITE_ERROR", "capture failed")

    monkeypatch.setattr(agent_runtime, "create_checkpoint", failing_capture)
    stream = io.StringIO()
    exit_code = main(
        ["run", "--workspace", str(tmp_path), "do the task"],
        environ={"DEEPSEEK_API_KEY": SENSITIVE_SENTINEL},
        console=Console(file=stream, force_terminal=False, color_system=None, width=200),
    )

    output = stream.getvalue()
    assert exit_code == 1
    assert "CHECKPOINT_WRITE_ERROR" in output
    assert "termination=checkpoint_error" in output
    # The run stopped before the model was contacted, so no tool ever ran.
    assert "TOOL:" not in output
    assert SENSITIVE_SENTINEL not in output


def test_run_command_accepts_an_explicit_opt_out(tmp_path: Path) -> None:
    from proofcoder.cli import build_parser

    parser = build_parser()
    with_flag = parser.parse_args(["run", "--workspace", str(tmp_path), "--no-checkpoint", "task"])
    without_flag = parser.parse_args(["run", "--workspace", str(tmp_path), "task"])

    assert with_flag.no_checkpoint is True
    assert without_flag.no_checkpoint is False


def test_capture_event_records_coverage_in_the_trace(tmp_path: Path) -> None:
    from proofcoder.agent import AgentLoop
    from proofcoder.llm.scripted import ScriptedClient
    from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
    from proofcoder.trace import TraceRecorder

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN_NAME=never-capture-this\n", encoding="utf-8")
    resources = _resources(tmp_path)
    capture = resources.checkpoint
    recorder = TraceRecorder(tmp_path, "e" * 32)
    try:
        AgentLoop(
            client=ScriptedClient(
                [
                    ModelResponse(
                        content="done",
                        reasoning_content=None,
                        finish_reason="tool_calls",
                        usage=None,
                        tool_calls=(
                            ToolCall(
                                id="call-finish",
                                function=FunctionCall(
                                    name="finish_task",
                                    arguments='{"summary": "nothing to do"}',
                                ),
                            ),
                        ),
                    )
                ]
            ),
            registry=resources.registry,
            workspace=tmp_path,
            system_prompt="system",
            max_steps=2,
            event_sink=recorder,
            run_id_factory=lambda: "e" * 32,
            trace_path=recorder.trace_path,
            checkpoint=capture,
        ).run("task")
    finally:
        recorder.close()
        resources.close()

    events = read_trace(tmp_path, "e" * 32).events
    checkpoint_events = [event for event in events if event.event_type is EventType.CHECKPOINT]
    assert len(checkpoint_events) == 1
    # The capture is reported before anything can change the workspace.
    assert events[0].event_type is EventType.TASK
    assert events[1].event_type is EventType.CHECKPOINT
    payload = checkpoint_events[0].payload
    assert payload["captured"] is True
    assert payload["uncovered"] == {"sensitive": 1}
    assert "never-capture-this" not in checkpoint_events[0].to_json()


def test_a_run_can_be_rolled_back_end_to_end(tmp_path: Path) -> None:
    from proofcoder.protocol import FunctionCall, ToolCall

    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    resources = _resources(tmp_path)
    try:
        # One real tool write, then one write by something other than the tools.
        result = resources.registry.dispatch(
            ToolCall(
                id="call-edit",
                function=FunctionCall(
                    name="replace_in_file",
                    arguments='{"path": "app.py", "old_text": "1", "new_text": "2"}',
                ),
            )
        )
        assert result.ok
        (tmp_path / "generated.txt").write_text("build output\n", encoding="utf-8")
    finally:
        resources.close()

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    plan = plan_rollback(tmp_path, RUN_ID, tool_written_paths=tool_written_paths(tmp_path, RUN_ID))
    rollback = apply_rollback(tmp_path, plan)

    assert rollback.complete
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert not (tmp_path / "generated.txt").exists()


def test_termination_reason_is_a_stable_value() -> None:
    assert TerminationReason.CHECKPOINT_ERROR.value == "checkpoint_error"


def test_every_destructive_tool_change_is_undone_by_rollback(tmp_path: Path) -> None:
    """The stage G exit criterion: each new tool's damage is fully reversible."""

    from proofcoder.protocol import FunctionCall, ToolCall

    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    (tmp_path / "doomed.txt").write_text("doomed\n", encoding="utf-8")
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "spare").mkdir()

    resources = _resources(tmp_path)
    try:
        import json

        for name, arguments in [
            ("delete_path", {"path": "doomed.txt"}),
            ("delete_path", {"path": "spare"}),
            ("move_path", {"source": "old/mod.py", "destination": "moved.py"}),
            ("make_directory", {"path": "fresh/nested"}),
            ("create_file", {"path": "keep.txt", "content": "rewritten\n", "overwrite": True}),
            (
                "patch_file",
                {"path": "moved.py", "edits": [{"old_text": "1", "new_text": "2"}]},
            ),
        ]:
            result = resources.registry.dispatch(
                ToolCall(
                    id=f"call-{name}",
                    function=FunctionCall(name=name, arguments=json.dumps(arguments)),
                )
            )
            assert result.ok, (name, arguments, result.error)
    finally:
        resources.close()

    # Everything the tools did, from every category the stage added.
    assert not (tmp_path / "doomed.txt").exists()
    assert not (tmp_path / "spare").exists()
    assert (tmp_path / "moved.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "fresh" / "nested").is_dir()
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "rewritten\n"

    rollback = apply_rollback(
        tmp_path,
        plan_rollback(tmp_path, RUN_ID, tool_written_paths=tool_written_paths(tmp_path, RUN_ID)),
    )

    assert rollback.complete
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert (tmp_path / "doomed.txt").read_text(encoding="utf-8") == "doomed\n"
    assert (tmp_path / "old" / "mod.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "spare").is_dir()
    assert not (tmp_path / "moved.py").exists()
    assert not (tmp_path / "fresh").exists()
