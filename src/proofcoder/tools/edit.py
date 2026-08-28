"""Safe workspace file creation and exact text replacement tools."""

from __future__ import annotations

import codecs
import difflib
import os
from collections.abc import Mapping
from pathlib import Path

from proofcoder.safety.paths import (
    WorkspacePathError,
    resolve_workspace_file,
    resolve_workspace_new_file,
)
from proofcoder.safety.writes import (
    FileSnapshot,
    commit_new_file,
    commit_replacement,
    discard_temporary_file,
    snapshot_still_matches,
    stage_temporary_file,
)
from proofcoder.tools.base import RiskLevel, ToolDefinition, ToolResult
from proofcoder.tools.files import MAX_FILE_SIZE_BYTES, _looks_binary

MAX_CONTENT_SIZE_BYTES = 1024 * 1024
MAX_REPLACEMENTS = 100
MAX_DIFF_LINES = 200
MAX_DIFF_CHARACTERS = 32 * 1024


def create_create_file_tool(workspace: Path) -> ToolDefinition:
    """Create a UTF-8 file creator bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _create_file(workspace_root, arguments)

    return ToolDefinition(
        name="create_file",
        description=(
            "Create one new, non-sensitive UTF-8 workspace file. The parent directory must "
            "already exist. Existing files, directories, and symlinks are never overwritten. "
            "Content is limited to 1 MiB and the returned unified diff may be truncated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path for the new file.",
                    "minLength": 1,
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 text content; an empty string is allowed.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        execute=execute,
        modifies_workspace=True,
        risk_level=RiskLevel.WRITE,
    )


def create_replace_in_file_tool(workspace: Path) -> ToolDefinition:
    """Create an exact UTF-8 text replacement tool bound to one workspace."""

    workspace_root = workspace.resolve(strict=True)

    def execute(arguments: Mapping[str, object]) -> ToolResult:
        return _replace_in_file(workspace_root, arguments)

    return ToolDefinition(
        name="replace_in_file",
        description=(
            "Replace an exact occurrence in one existing, non-sensitive UTF-8 workspace file. "
            "Use LF in multiline arguments even for CRLF or CR files. Matching must equal "
            "expected_replacements (default 1), and the returned unified diff may be truncated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative existing file to modify.",
                    "minLength": 1,
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to replace; LF matches LF, CRLF, or CR newlines.",
                    "minLength": 1,
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text; an empty string deletes the match.",
                },
                "expected_replacements": {
                    "type": "integer",
                    "description": "Required number of non-overlapping exact matches.",
                    "minimum": 1,
                    "maximum": MAX_REPLACEMENTS,
                    "default": 1,
                },
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        execute=execute,
        modifies_workspace=True,
        risk_level=RiskLevel.WRITE,
    )


def _create_file(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    path = str(arguments["path"])
    content = str(arguments["content"])
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_CONTENT_SIZE_BYTES:
        return ToolResult.failure(
            "CONTENT_TOO_LARGE",
            f"content exceeds the {MAX_CONTENT_SIZE_BYTES}-byte write limit",
            retryable=True,
        )

    try:
        target, relative_path = resolve_workspace_new_file(workspace_root, path)
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)

    diff, diff_stats, truncated = _build_diff(
        relative_path=relative_path,
        before_text="",
        after_text=content,
        before_bytes=0,
        after_bytes=len(encoded),
        replacement_count=0,
        creating=True,
    )

    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(target, encoded)
        try:
            commit_new_file(temporary, target)
        except FileExistsError:
            return ToolResult.failure(
                "PATH_ALREADY_EXISTS",
                "target path appeared during creation and was not overwritten",
                retryable=True,
            )
        except OSError:
            if os.path.lexists(target):
                return ToolResult.failure(
                    "PATH_ALREADY_EXISTS",
                    "target path appeared during creation and was not overwritten",
                    retryable=True,
                )
            return ToolResult.failure(
                "ATOMIC_WRITE_ERROR",
                "could not atomically publish the staged file",
                retryable=True,
            )
    except OSError:
        return ToolResult.failure(
            "ATOMIC_WRITE_ERROR",
            "could not stage the complete file content",
            retryable=True,
        )
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)

    return ToolResult.success(
        {
            "path": relative_path,
            "bytes_written": len(encoded),
            "encoding": "utf-8",
            "diff": diff,
            "diff_stats": diff_stats,
        },
        truncated=truncated,
    )


def _replace_in_file(workspace_root: Path, arguments: Mapping[str, object]) -> ToolResult:
    path = str(arguments["path"])
    old_text = str(arguments["old_text"])
    new_text = str(arguments["new_text"])
    expected_replacements = int(arguments["expected_replacements"])

    if (
        len(old_text.encode("utf-8")) > MAX_CONTENT_SIZE_BYTES
        or len(new_text.encode("utf-8")) > MAX_CONTENT_SIZE_BYTES
    ):
        return ToolResult.failure(
            "CONTENT_TOO_LARGE",
            f"replacement arguments exceed the {MAX_CONTENT_SIZE_BYTES}-byte limit",
            retryable=True,
        )

    try:
        target, relative_path = resolve_workspace_file(workspace_root, path)
        before_metadata = target.stat()
        if before_metadata.st_size > MAX_FILE_SIZE_BYTES:
            return ToolResult.failure(
                "FILE_TOO_LARGE",
                f"file exceeds the {MAX_FILE_SIZE_BYTES}-byte edit limit",
                retryable=True,
            )
        raw = target.read_bytes()
        after_metadata = target.stat()
    except WorkspacePathError as error:
        return ToolResult.failure(error.code, str(error), retryable=True)
    except OSError:
        return ToolResult.failure(
            "FILE_CHANGED",
            "file could not be read consistently; read it again before retrying",
            retryable=True,
        )

    if len(raw) > MAX_FILE_SIZE_BYTES:
        return ToolResult.failure(
            "FILE_TOO_LARGE",
            f"file exceeds the {MAX_FILE_SIZE_BYTES}-byte edit limit",
            retryable=True,
        )
    if _looks_binary(raw):
        return ToolResult.failure(
            "BINARY_FILE",
            "binary files cannot be modified as UTF-8 text",
            retryable=True,
        )

    has_bom = raw.startswith(codecs.BOM_UTF8)
    try:
        before_text = raw.decode("utf-8-sig" if has_bom else "utf-8")
    except UnicodeDecodeError:
        return ToolResult.failure(
            "DECODE_ERROR",
            "file is not valid UTF-8 text",
            retryable=True,
        )

    snapshot = FileSnapshot.capture(before_metadata, raw)
    if not snapshot.matches_metadata(after_metadata):
        return ToolResult.failure(
            "FILE_CHANGED",
            "file changed while it was being read; read it again before retrying",
            retryable=True,
        )

    normalized_text, original_boundaries = _normalize_with_boundaries(before_text)
    normalized_old = _normalize_newlines(old_text)
    match_spans = _find_non_overlapping(normalized_text, normalized_old)
    actual_replacements = len(match_spans)
    if actual_replacements == 0:
        return ToolResult.failure(
            "MATCH_NOT_FOUND",
            "old_text did not match the current file content",
            retryable=True,
        )
    if actual_replacements != expected_replacements:
        return ToolResult.failure(
            "AMBIGUOUS_MATCH",
            f"old_text matched {actual_replacements} times; expected "
            f"{expected_replacements} replacements",
            retryable=True,
        )

    dominant_newline = _dominant_newline(before_text)
    pieces: list[str] = []
    previous_original_end = 0
    for normalized_start, normalized_end in match_spans:
        original_start = original_boundaries[normalized_start]
        original_end = original_boundaries[normalized_end]
        matched_original = before_text[original_start:original_end]
        insertion_newline = _dominant_newline(matched_original, fallback=dominant_newline)
        replacement = _normalize_newlines(new_text).replace("\n", insertion_newline)
        pieces.append(before_text[previous_original_end:original_start])
        pieces.append(replacement)
        previous_original_end = original_end
    pieces.append(before_text[previous_original_end:])
    after_text = _preserve_terminal_newline("".join(pieces), before_text)

    body = after_text.encode("utf-8")
    encoded = codecs.BOM_UTF8 + body if has_bom else body
    if len(encoded) > MAX_FILE_SIZE_BYTES:
        return ToolResult.failure(
            "CONTENT_TOO_LARGE",
            f"result exceeds the {MAX_FILE_SIZE_BYTES}-byte write limit",
            retryable=True,
        )

    diff, diff_stats, truncated = _build_diff(
        relative_path=relative_path,
        before_text=before_text,
        after_text=after_text,
        before_bytes=len(raw),
        after_bytes=len(encoded),
        replacement_count=actual_replacements,
        creating=False,
    )

    if encoded == raw:
        return ToolResult.success(
            {
                "path": relative_path,
                "bytes_written": len(encoded),
                "encoding": "utf-8-sig" if has_bom else "utf-8",
                "replacements": actual_replacements,
                "diff": diff,
                "diff_stats": diff_stats,
            },
            truncated=truncated,
        )

    temporary: Path | None = None
    try:
        temporary = stage_temporary_file(target, encoded, mode=before_metadata.st_mode)
        if not snapshot_still_matches(target, snapshot):
            return ToolResult.failure(
                "FILE_CHANGED",
                "file changed after it was read and was not overwritten",
                retryable=True,
            )
        try:
            commit_replacement(temporary, target)
        except OSError:
            return ToolResult.failure(
                "ATOMIC_WRITE_ERROR",
                "could not atomically replace the original file",
                retryable=True,
            )
        temporary = None
    except OSError:
        return ToolResult.failure(
            "ATOMIC_WRITE_ERROR",
            "could not stage the complete replacement content",
            retryable=True,
        )
    finally:
        if temporary is not None:
            discard_temporary_file(temporary)

    return ToolResult.success(
        {
            "path": relative_path,
            "bytes_written": len(encoded),
            "encoding": "utf-8-sig" if has_bom else "utf-8",
            "replacements": actual_replacements,
            "diff": diff,
            "diff_stats": diff_stats,
        },
        truncated=truncated,
    )


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_with_boundaries(text: str) -> tuple[str, list[int]]:
    normalized: list[str] = []
    boundaries = [0]
    index = 0
    while index < len(text):
        if text[index] == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                index += 2
            else:
                index += 1
            normalized.append("\n")
        else:
            normalized.append(text[index])
            index += 1
        boundaries.append(index)
    return "".join(normalized), boundaries


def _find_non_overlapping(text: str, needle: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return matches
        end = found + len(needle)
        matches.append((found, end))
        start = end


def _dominant_newline(text: str, *, fallback: str = "\n") -> str:
    counts = {"\r\n": 0, "\n": 0, "\r": 0}
    first_seen: dict[str, int] = {}
    index = 0
    order = 0
    while index < len(text):
        style: str | None = None
        if text[index] == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                style = "\r\n"
                index += 2
            else:
                style = "\r"
                index += 1
        elif text[index] == "\n":
            style = "\n"
            index += 1
        else:
            index += 1
        if style is not None:
            counts[style] += 1
            first_seen.setdefault(style, order)
            order += 1

    present = [style for style, count in counts.items() if count]
    if not present:
        return fallback
    return min(present, key=lambda style: (-counts[style], first_seen[style]))


def _terminal_newline(text: str) -> str | None:
    if text.endswith("\r\n"):
        return "\r\n"
    if text.endswith("\r"):
        return "\r"
    if text.endswith("\n"):
        return "\n"
    return None


def _preserve_terminal_newline(after_text: str, before_text: str) -> str:
    original_terminal = _terminal_newline(before_text)
    if original_terminal is None:
        return after_text.rstrip("\r\n")

    replacement_terminal = _terminal_newline(after_text)
    if replacement_terminal is None:
        return after_text + original_terminal
    return after_text[: -len(replacement_terminal)] + original_terminal


def _build_diff(
    *,
    relative_path: str,
    before_text: str,
    after_text: str,
    before_bytes: int,
    after_bytes: int,
    replacement_count: int,
    creating: bool,
) -> tuple[str, dict[str, int], bool]:
    before_display = _normalize_newlines(before_text)
    after_display = _normalize_newlines(after_text)
    before_lines = before_display.splitlines(keepends=True)
    after_lines = after_display.splitlines(keepends=True)
    from_path = "/dev/null" if creating else f"a/{relative_path}"
    to_path = f"b/{relative_path}"
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=from_path,
            tofile=to_path,
            lineterm="\n",
        )
    )
    complete_diff = "".join(line if line.endswith("\n") else f"{line}\n" for line in diff_lines)
    if creating and not complete_diff:
        complete_diff = f"--- {from_path}\n+++ {to_path}\n"
    added_lines = sum(
        1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
    )
    removed_lines = sum(
        1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
    )
    bounded_diff, truncated = _bound_diff(complete_diff)
    return (
        bounded_diff,
        {
            "added_lines": added_lines,
            "removed_lines": removed_lines,
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "replacement_count": replacement_count,
        },
        truncated,
    )


def _bound_diff(diff: str) -> tuple[str, bool]:
    lines = diff.splitlines(keepends=True)
    if len(lines) <= MAX_DIFF_LINES and len(diff) <= MAX_DIFF_CHARACTERS:
        return diff, False

    line_limited = len(lines) >= MAX_DIFF_LINES
    if line_limited:
        head_count = (MAX_DIFF_LINES - 1) // 2
        tail_count = MAX_DIFF_LINES - head_count - 1
        head_source = "".join(lines[:head_count])
        tail_source = "".join(lines[-tail_count:])
        omitted_lines = len(lines) - head_count - tail_count
    else:
        head_source = diff
        tail_source = diff
        omitted_lines = 0

    marker = ""
    bounded_head = head_source
    bounded_tail = tail_source if line_limited else ""
    for _ in range(4):
        available = max(0, MAX_DIFF_CHARACTERS - len(marker))
        head_budget = available // 2
        tail_budget = available - head_budget
        bounded_head = head_source[:head_budget]
        bounded_tail = tail_source[-tail_budget:] if tail_budget else ""
        omitted_characters = len(diff) - len(bounded_head) - len(bounded_tail)
        marker = (
            f"... diff truncated: {omitted_lines} complete lines and "
            f"{omitted_characters} characters omitted ...\n"
        )

    available = max(0, MAX_DIFF_CHARACTERS - len(marker))
    head_budget = available // 2
    tail_budget = available - head_budget
    bounded_head = head_source[:head_budget]
    bounded_tail = tail_source[-tail_budget:] if tail_budget else ""
    return f"{bounded_head}{marker}{bounded_tail}", True
