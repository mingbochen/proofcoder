"""Byte-level golden snapshot guarding trace.jsonl against presentation changes.

The trace is the audited artifact. Terminal rendering must never alter it, so this
module runs one fully deterministic scripted trajectory and compares the produced
``trace.jsonl`` byte for byte with a committed golden file.

Regenerate intentionally with::

    PROOFCODER_UPDATE_GOLDEN=1 pytest tests/unit/test_trace_golden.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from proofcoder.agent import AgentLoop
from proofcoder.events import EventType
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import FunctionCall, ModelResponse, ToolCall
from proofcoder.tools.base import RiskLevel, ToolDefinition, ToolResult
from proofcoder.tools.edit import create_create_file_tool, create_replace_in_file_tool
from proofcoder.tools.files import create_list_files_tool, create_read_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry
from proofcoder.trace import TraceRecorder, read_trace

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "trace_golden.jsonl"
GOLDEN_RUN_ID = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
GOLDEN_TIME = datetime(2026, 8, 31, 4, 5, 6, 789012, tzinfo=UTC)
GOLDEN_TASK = "Fix the addition helper and record a short note."
VERIFY_ARGV = ["python", "-m", "unittest", "discover", "-s", "tests"]

CALC_SOURCE = (
    '"""Small calculator helpers."""\n\n\n'
    "def add(left: int, right: int) -> int:\n"
    "    return left - right\n"
)
CALC_TEST_SOURCE = (
    "import unittest\n\n"
    "from calc import add\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_add(self) -> None:\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
NOTES_SOURCE = "# Notes\n\nFixed the addition helper.\n"


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(
            name=name,
            arguments=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def _response(*calls: ToolCall, content: str | None = None) -> ModelResponse:
    return ModelResponse(
        content=content,
        reasoning_content="golden reasoning that must never reach the trace",
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=tuple(calls),
    )


def _stub_run_command_tool() -> ToolDefinition:
    """Return a ``run_command`` stand-in with fixed timings and byte counts.

    The real tool spawns a subprocess whose duration and output sizes differ per
    machine, which cannot appear in a byte-exact golden file. This stub keeps the
    exact result shape that VerificationTracker and the event payload builders read.
    """

    def execute(arguments: object) -> ToolResult:
        return ToolResult.success(
            {
                "argv": list(VERIFY_ARGV),
                "cwd": ".",
                "command_kind": "test",
                "exit_code": 0,
                "stdout": "",
                "stderr": "OK\n",
                "stdout_bytes": 0,
                "stderr_bytes": 3,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "timed_out": False,
                "audit_truncated": False,
            }
        )

    return ToolDefinition(
        name="run_command",
        description="Deterministic offline stand-in for the local command tool.",
        parameters={
            "type": "object",
            "properties": {
                "argv": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "integer"},
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        execute=execute,
        risk_level=RiskLevel.EXECUTE,
    )


def _build_workspace(root: Path) -> None:
    (root / "calc.py").write_text(CALC_SOURCE, encoding="utf-8", newline="")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(CALC_TEST_SOURCE, encoding="utf-8", newline="")


def _golden_registry(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(create_list_files_tool(root))
    registry.register(create_read_file_tool(root))
    registry.register(create_create_file_tool(root))
    registry.register(create_replace_in_file_tool(root))
    registry.register(_stub_run_command_tool())
    registry.register(create_finish_task_tool(root))
    return registry


def _golden_client() -> ScriptedClient:
    return ScriptedClient(
        [
            # One batch with two calls exercises multi tool-call pairing.
            _response(
                _call("call-list", "list_files", {"path": ".", "max_depth": 2}),
                _call("call-read", "read_file", {"path": "calc.py"}),
            ),
            # A miss produces a failed tool result plus its warning event.
            _response(
                _call(
                    "call-miss",
                    "replace_in_file",
                    {"path": "calc.py", "old_text": "left * right", "new_text": "left + right"},
                ),
                content="The first replacement guess did not match.",
            ),
            _response(
                _call(
                    "call-edit",
                    "replace_in_file",
                    {"path": "calc.py", "old_text": "left - right", "new_text": "left + right"},
                ),
                content="Applying the corrected replacement.",
            ),
            _response(
                _call("call-create", "create_file", {"path": "notes.md", "content": NOTES_SOURCE})
            ),
            _response(_call("call-verify", "run_command", {"argv": VERIFY_ARGV})),
            _response(
                _call(
                    "call-finish",
                    "finish_task",
                    {
                        "summary": "Corrected add() and added notes.md.",
                        "changed_files": ["calc.py", "notes.md"],
                        "verification_command": VERIFY_ARGV,
                    },
                ),
                content="Tests pass.",
            ),
        ]
    )


def _run_golden_trajectory(workspace: Path) -> bytes:
    """Run the fixed trajectory and return the raw recorded trace bytes."""

    _build_workspace(workspace)
    recorder = TraceRecorder(workspace, GOLDEN_RUN_ID)
    try:
        AgentLoop(
            client=_golden_client(),
            registry=_golden_registry(workspace),
            workspace=workspace,
            system_prompt="golden system prompt",
            max_steps=8,
            clock=lambda: 100.0,
            sleep=lambda _seconds: None,
            random_value=lambda: 0.0,
            event_sink=recorder,
            run_id_factory=lambda: GOLDEN_RUN_ID,
            event_clock=lambda: GOLDEN_TIME,
            trace_path=recorder.trace_path,
        ).run(GOLDEN_TASK)
    finally:
        recorder.close()
    return (workspace / Path(recorder.trace_path)).read_bytes()


def test_trace_jsonl_matches_committed_golden_bytes(tmp_path: Path) -> None:
    produced = _run_golden_trajectory(tmp_path)

    if os.environ.get("PROOFCODER_UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_bytes(produced)

    assert GOLDEN_PATH.is_file(), (
        f"golden trace is missing at {GOLDEN_PATH}; regenerate with PROOFCODER_UPDATE_GOLDEN=1"
    )
    expected = GOLDEN_PATH.read_bytes()
    if produced != expected:
        produced_lines = produced.decode("utf-8").splitlines()
        expected_lines = expected.decode("utf-8").splitlines()
        difference = next(
            (
                f"line {index + 1}\n  expected: {expected_line}\n  produced: {produced_line}"
                for index, (expected_line, produced_line) in enumerate(
                    zip(expected_lines, produced_lines, strict=False)
                )
                if expected_line != produced_line
            ),
            f"line count expected={len(expected_lines)} produced={len(produced_lines)}",
        )
        raise AssertionError(
            "trace.jsonl changed. Terminal rendering must never alter the persisted "
            f"trace; regenerate only for an intended trace change.\n{difference}"
        )


def test_golden_trace_covers_every_event_type_and_hides_reasoning(tmp_path: Path) -> None:
    raw = _run_golden_trajectory(tmp_path)

    assert b"golden reasoning" not in raw
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    trace = read_trace(tmp_path, GOLDEN_RUN_ID)
    observed = {event.event_type for event in trace.events}
    assert observed == set(EventType), (
        f"golden trajectory lost coverage of {set(EventType) - observed}"
    )
    assert trace.events[-1].payload["completion_status"] == "completed_verified"
    assert trace.events[-1].payload["changed_files"] == ["calc.py", "notes.md"]
