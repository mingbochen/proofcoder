"""Terminal-only rendering of already sanitized events.

These tests exercise presentation exclusively. Result payloads are built through the
real ``summarize_tool_result`` so the rendered line is checked against what the trace
actually stores, not against a hand-written dictionary.
"""

from __future__ import annotations

from proofcoder.events import (
    EventType,
    RunEvent,
    TerminalSink,
    render_terminal_event,
    summarize_tool_arguments,
    summarize_tool_result,
)
from proofcoder.tools.base import ToolResult

RUN_ID = "d" * 32
TIMESTAMP = "2026-08-28T01:02:03.456789Z"


def _event(
    event_type: EventType,
    payload: dict[str, object],
    *,
    step: int = 1,
    sequence: int = 1,
) -> RunEvent:
    return RunEvent(
        run_id=RUN_ID,
        sequence=sequence,
        step=step,
        timestamp=TIMESTAMP,
        event_type=event_type,
        payload=payload,
    )


def _result_line(tool_name: str, result: ToolResult, call_id: str = "call-1") -> str | None:
    """Render one result through the production payload builder."""

    payload = summarize_tool_result(tool_name, call_id, result)
    return render_terminal_event(_event(EventType.TOOL_RESULT, payload))


def test_list_files_result_renders_counts_and_omitted_entries() -> None:
    result = ToolResult.success(
        {
            "queried_path": "src",
            "returned_count": 20,
            "total_matched_count": 34,
            "truncated_count": 14,
        },
        truncated=True,
    )

    assert _result_line("list_files", result) == (
        "RESULT: list_files ok id=call-1 path=src entries=20/34 omitted=14 truncated=true"
    )


def test_search_text_result_renders_query_and_more_matches_flag() -> None:
    capped = ToolResult.success(
        {
            "query": "format_message",
            "queried_path": ".",
            "returned_count": 50,
            "more_matches_available": True,
        }
    )
    complete = ToolResult.success(
        {
            "query": "format_message",
            "queried_path": ".",
            "returned_count": 3,
            "more_matches_available": False,
        }
    )

    assert _result_line("search_text", capped) == (
        "RESULT: search_text ok id=call-1 query=format_message path=. matches=50 more=true"
    )
    assert _result_line("search_text", complete) == (
        "RESULT: search_text ok id=call-1 query=format_message path=. matches=3"
    )


def test_read_file_result_renders_line_range_and_bytes() -> None:
    result = ToolResult.success(
        {
            "path": "message.py",
            "total_lines": 9,
            "actual_start_line": 1,
            "actual_end_line": 9,
            "returned_line_count": 9,
            "returned_bytes": 253,
            "encoding": "utf-8",
            "newline_style": "lf",
        },
        duration_ms=4,
    )

    assert _result_line("read_file", result) == (
        "RESULT: read_file ok id=call-1 path=message.py lines=1-9/9 bytes=253 duration_ms=4"
    )


def test_replace_in_file_result_renders_diff_stats_and_multiple_replacements() -> None:
    result = ToolResult.success(
        {
            "path": "settings.py",
            "bytes_written": 98,
            "encoding": "utf-8",
            "replacements": 3,
            "diff_stats": {"added_lines": 2, "removed_lines": 1},
        }
    )

    assert _result_line("replace_in_file", result) == (
        "RESULT: replace_in_file ok id=call-1 path=settings.py +2/-1 bytes=98 replacements=3"
    )


def test_create_file_result_without_diff_stats_omits_the_line_counter() -> None:
    result = ToolResult.success({"path": "notes.md", "bytes_written": 36, "encoding": "utf-8"})

    assert _result_line("create_file", result) == (
        "RESULT: create_file ok id=call-1 path=notes.md bytes=36"
    )


def test_run_command_result_renders_timeout_and_output_truncation() -> None:
    result = ToolResult.success(
        {
            "argv": ["pytest", "-q"],
            "cwd": ".",
            "command_kind": "test",
            "exit_code": None,
            "stdout_bytes": 4096,
            "stderr_bytes": 12,
            "stdout_truncated": True,
            "stderr_truncated": False,
            "timed_out": True,
            "audit_truncated": False,
        },
        duration_ms=10_000,
    )

    assert _result_line("run_command", result) == (
        "RESULT: run_command ok id=call-1 exit_code=none kind=test cwd=. "
        "out=4096B err=12B timed_out=true output_truncated=true duration_ms=10000"
    )


def test_finish_task_result_counts_changed_files_and_limitations() -> None:
    result = ToolResult.success(
        {
            "completion_status": "completed_unverified",
            "changed_files": ["a.py", "b.py"],
            "verification": None,
            "limitations": ["no test command was run"],
            "blocked_reason": None,
        }
    )

    assert _result_line("finish_task", result) == (
        "RESULT: finish_task ok id=call-1 completion=completed_unverified "
        "changed_files=2 limitations=1"
    )


def test_failed_result_renders_error_code_retryable_and_message() -> None:
    result = ToolResult.failure(
        "AMBIGUOUS_MATCH",
        "old_text matched 3 locations; provide more context",
        retryable=True,
        data={"path": "message.py"},
    )

    assert _result_line("replace_in_file", result) == (
        "RESULT: replace_in_file FAIL id=call-1 error=AMBIGUOUS_MATCH retryable=true "
        "path=message.py bytes=0 (old_text matched 3 locations; provide more context)"
    )


def test_unrecognized_tool_result_renders_only_the_common_fields() -> None:
    assert _result_line("probe", ToolResult.success({"value": 1})) == "RESULT: probe ok id=call-1"


def test_completion_event_is_not_rendered_to_the_terminal() -> None:
    event = _event(EventType.COMPLETION, {"completion_status": "completed_verified"})

    assert render_terminal_event(event) is None


def test_tool_call_rendering_drops_digests_and_leads_with_path() -> None:
    summary = summarize_tool_arguments(
        "replace_in_file",
        '{"path":"settings.py","old_text":"before","new_text":"after"}',
    )

    line = render_terminal_event(
        _event(
            EventType.TOOL_CALL,
            {"arguments": summary, "tool_call_id": "c1", "tool_name": "replace_in_file"},
        )
    )

    assert line == "TOOL: replace_in_file id=c1 path=settings.py new=5B old=6B"
    assert "sha256" not in line


def test_tool_call_rendering_uses_json_literals_and_clips_long_prose() -> None:
    flag_line = render_terminal_event(
        _event(
            EventType.TOOL_CALL,
            {
                "arguments": {"include_hidden": False, "path": "."},
                "tool_call_id": "c2",
                "tool_name": "list_files",
            },
        )
    )
    prose_line = render_terminal_event(
        _event(
            EventType.TOOL_CALL,
            {
                "arguments": {"summary": "word " * 40},
                "tool_call_id": "c3",
                "tool_name": "finish_task",
            },
        )
    )

    assert flag_line == "TOOL: list_files id=c2 path=. include_hidden=false"
    assert prose_line.endswith("...")
    # The full summary stays in the trace; only the displayed value is clipped.
    assert len(prose_line.split("summary=", 1)[1]) == 63


def test_rendering_tolerates_absent_arguments_stats_and_odd_changed_file_shapes() -> None:
    call_line = render_terminal_event(
        _event(EventType.TOOL_CALL, {"tool_call_id": "c1", "tool_name": "list_files"})
    )
    diff_line = render_terminal_event(
        _event(EventType.DIFF, {"path": "a.py", "preview": "--- a/a.py"})
    )
    termination_line = render_terminal_event(
        _event(EventType.TERMINATION, {"changed_files": "a.py", "trace_complete": False})
    )

    assert call_line == "TOOL: list_files id=c1"
    assert diff_line == "DIFF: path=a.py\n--- a/a.py"
    # A bare string must not be counted as a list of one-character file names.
    assert "  changed_files: none" in termination_line
    assert "(INCOMPLETE)" in termination_line
    assert "None" not in termination_line


def test_failed_result_without_a_retryable_flag_still_renders() -> None:
    # A replayed trace may carry a failure payload that predates or omits the flag.
    line = render_terminal_event(
        _event(
            EventType.TOOL_RESULT,
            {
                "success": False,
                "tool_call_id": "c1",
                "tool_name": "read_file",
                "error_code": "PATH_OUTSIDE_WORKSPACE",
            },
        )
    )

    assert line == (
        "RESULT: read_file FAIL id=c1 error=PATH_OUTSIDE_WORKSPACE path=none lines=0-0/0 bytes=0"
    )


def test_terminal_sink_groups_by_step_skips_empty_model_text_and_spaces_the_final_block() -> None:
    lines: list[str] = []
    sink = TerminalSink(lines.append)

    sink.emit(_event(EventType.TASK, {"task": "t"}, step=0, sequence=1))
    sink.emit(_event(EventType.MODEL, {"text": "first"}, step=1, sequence=2))
    sink.emit(_event(EventType.MODEL, {"text": "   "}, step=1, sequence=3))
    sink.emit(_event(EventType.MODEL, {"text": "second"}, step=2, sequence=4))
    sink.emit(_event(EventType.TERMINATION, {"trace_complete": True}, step=2, sequence=5))

    assert lines[:6] == [
        "TASK: t",
        "",
        "-- step 1 --",
        "MODEL: first",
        "",
        "-- step 2 --",
    ]
    assert lines[6] == "MODEL: second"
    assert lines[7] == ""
    assert lines[8].startswith("DONE: ")
    # A blank model message is skipped entirely rather than shown as a placeholder.
    assert not any("no visible text" in line for line in lines)
    assert len(lines) == 9
