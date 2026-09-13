"""Offline tests for the documentation consistency compliance checks.

Each test builds a small repository whose code, specification, README, ADRs, and
roadmap agree, then breaks one of them. Failures are therefore tied to a concrete
document change rather than to the live repository's current wording.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from proofcoder.compliance import (
    CheckStatus,
    ComplianceCheck,
    ComplianceReport,
    check_documentation_consistency,
    format_json,
    run_compliance,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SENSITIVE_SENTINEL = "never-echo-this-document-value"
# Written as an escape because ruff flags the full-width colon as confusable.
COLON = "\uff1a"
EM_DASH = "—"
DOCUMENTATION_CHECKS = (
    "documentation.adr_index",
    "documentation.adr_status",
    "documentation.roadmap_status",
    "documentation.spec_layout",
    "documentation.tools",
)
MISSING_PREFIX = "required document is missing, not a regular file, or unreadable: "
LAYOUT_MISSING_PREFIX = "specification section 5.1 lists a path that does not exist: "

REGISTRATION = """from proofcoder.tools.sample import create_alpha_tool, create_beta_tool


def create_resources(registry, workspace):
    registry.register(create_alpha_tool(workspace))
    registry.register(create_beta_tool(workspace))
"""

TOOLS = """BETA_NAME = "beta_tool"


def create_alpha_tool(workspace):
    return ToolDefinition(name="alpha_tool", description="")


def create_beta_tool(workspace):
    return ToolDefinition(name=BETA_NAME, description="")
"""

# "+-- ", "\\-- ", and "|   " stand for the tree's box-drawing prefixes; see _tree.
LAYOUT = """proofcoder/
+-- README.md
+-- docs/
|   +-- DEVELOPMENT_SPEC.md
|   \\-- adr/
\\-- src/proofcoder/
    \\-- agent_runtime.py"""

ROADMAP = (
    "# Roadmap\n\n"
    "| 阶段 | 状态 |\n| --- | --- |\n| A | 已完成 |\n| B | 阻塞 waiting for review |\n\n"
    "| 小项 | 状态 | PR |\n| --- | --- | --- |\n"
    "| 1 | 已完成 | [#1](https://example.invalid/1) |\n"
    f"| 2 | 未开始 | {EM_DASH} |\n"
)


def _tree(text: str) -> str:
    return text.replace("|   ", "│   ").replace("+-- ", "├── ").replace("\\-- ", "└── ")


def _specification(
    *,
    layout: str = LAYOUT,
    tools: tuple[str, ...] = ("alpha_tool", "beta_tool"),
) -> str:
    headings = "\n\n".join(
        f"### 7.{index} `{name}`\n\nDetails." for index, name in enumerate(tools, start=1)
    )
    return (
        "# Specification\n\n### 5.1 Layout\n\n"
        f"```text\n{_tree(layout)}\n```\n\n"
        f"## 7. Tools\n\n{headings}\n\n## 8. Loop\n"
    )


def _readme(rows: tuple[str, ...] = ("alpha_tool", "beta_tool")) -> str:
    body = "\n".join(f"| `{name}` | purpose |" for name in rows)
    return f"# Project\n\n## Local Tools\n\n| Tool | Purpose |\n| --- | --- |\n{body}\n\n## Next\n"


def _adr(number: str, *, status: str = "已接受", title_number: str | None = None) -> str:
    return (
        f"# ADR-{title_number or number}{COLON}Decision\n\n"
        f"- 状态{COLON}{status}\n- 日期{COLON}2026-01-01\n"
    )


def _adr_index(rows: tuple[str, ...]) -> str:
    table = "| 编号 | 标题 | 状态 | 日期 |\n| --- | --- | --- | --- |\n" + "\n".join(rows)
    return f"# Records\n\n## Index\n\n{table}\n"


def _index_row(number: str, filename: str, status: str = "已接受") -> str:
    return f"| [{number}]({filename}) | Decision | {status} | 2026-01-01 |"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repository(root: Path) -> Path:
    _write(root / "src/proofcoder/agent_runtime.py", REGISTRATION)
    _write(root / "src/proofcoder/tools/sample.py", TOOLS)
    _write(root / "docs/DEVELOPMENT_SPEC.md", _specification())
    _write(root / "README.md", _readme())
    _write(root / "docs/ROADMAP.md", ROADMAP)
    _write(
        root / "docs/adr/0000-template.md",
        f"# ADR-NNNN{COLON}Title\n\n- 状态{COLON}提议 | 已接受\n",
    )
    _write(root / "docs/adr/0001-first-decision.md", _adr("0001"))
    _write(
        root / "docs/adr/README.md",
        _adr_index((_index_row("0001", "0001-first-decision.md"),)),
    )
    return root


def _checks(root: Path, check_id: str) -> tuple[ComplianceCheck, ...]:
    records = tuple(
        check for check in check_documentation_consistency(root) if check.check_id == check_id
    )
    assert records, check_id
    return records


def _failures(root: Path, check_id: str) -> list[str]:
    return [check.message for check in _checks(root, check_id) if check.status is CheckStatus.FAIL]


# ---------- whole repository ----------


def test_current_repository_documentation_is_consistent() -> None:
    checks = check_documentation_consistency(PROJECT_ROOT)
    report_ids = {check.check_id for check in run_compliance(PROJECT_ROOT).checks}

    assert sorted(check.check_id for check in checks) == list(DOCUMENTATION_CHECKS)
    assert [check.message for check in checks if check.status is not CheckStatus.PASS] == []
    assert set(DOCUMENTATION_CHECKS) <= report_ids


def test_consistent_repository_passes_every_documentation_check(tmp_path: Path) -> None:
    checks = check_documentation_consistency(_repository(tmp_path))

    assert sorted(check.check_id for check in checks) == list(DOCUMENTATION_CHECKS)
    assert all(check.status is CheckStatus.PASS for check in checks)
    assert _checks(tmp_path, "documentation.tools")[0].message.startswith("2 registered tools")
    assert _checks(tmp_path, "documentation.spec_layout")[0].message == (
        "all 6 paths in the specification section 5.1 layout exist"
    )


def test_missing_documents_are_failures_not_infrastructure_errors(tmp_path: Path) -> None:
    checks = check_documentation_consistency(tmp_path)

    assert sorted({check.check_id for check in checks}) == list(DOCUMENTATION_CHECKS)
    assert all(check.status is CheckStatus.FAIL for check in checks)


# ---------- tools ----------


def test_specification_tool_drift_is_reported_in_both_directions(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(root / "docs/DEVELOPMENT_SPEC.md", _specification(tools=("alpha_tool", "gamma_tool")))

    records = _checks(root, "documentation.tools")
    messages = [check.message for check in records]
    extra = next(check for check in records if check.message.endswith("gamma_tool"))

    assert all(check.status is CheckStatus.FAIL for check in records)
    assert messages == [
        "specification section 7 documents a tool that is not registered: gamma_tool",
        "registered tool is not documented in specification section 7: beta_tool",
    ]
    assert extra.path == "docs/DEVELOPMENT_SPEC.md"
    assert extra.line is not None


def test_readme_duplicate_and_malformed_tool_rows_are_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    rows = ("alpha_tool", "alpha_tool", "not a name", "beta_tool")
    _write(root / "README.md", _readme(rows=rows))

    assert _failures(root, "documentation.tools") == [
        "the README tool table documents the same tool more than once: alpha_tool",
        "the README tool table lists a tool entry that is not a plain identifier",
    ]


def test_readme_without_tool_section_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(root / "README.md", "# Project\n\n## Usage\n\nNothing here.\n")

    assert _failures(root, "documentation.tools") == ["README has no '## Local Tools' section"]


def test_readme_tool_section_may_be_the_last_section(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(root / "README.md", _readme().removesuffix("\n## Next\n"))

    assert _failures(root, "documentation.tools") == []


def test_missing_specification_and_readme_are_reported_for_tools(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "docs/DEVELOPMENT_SPEC.md").unlink()
    (root / "README.md").unlink()

    assert _failures(root, "documentation.tools") == [
        f"{MISSING_PREFIX}docs/DEVELOPMENT_SPEC.md",
        f"{MISSING_PREFIX}README.md",
    ]


@pytest.mark.parametrize(
    ("registration", "expected"),
    [
        ("value = 1\n", "no tool registrations were found"),
        ("def broken(:\n", "tool registration module is not valid Python"),
        (
            "def create_local_tool(workspace):\n    return None\n\n\n"
            "def build(registry, workspace):\n"
            "    registry.register(create_local_tool(workspace))\n",
            "tool factory is not imported from a proofcoder module: create_local_tool",
        ),
        (
            "from other.tools import create_alpha_tool\n\n\n"
            "def build(registry, workspace):\n"
            "    registry.register(create_alpha_tool(workspace))\n",
            "tool factory is not imported from a proofcoder module: create_alpha_tool",
        ),
    ],
)
def test_registration_problems_are_reported(
    tmp_path: Path, registration: str, expected: str
) -> None:
    root = _repository(tmp_path)
    _write(root / "src/proofcoder/agent_runtime.py", registration)

    assert _failures(root, "documentation.tools") == [expected]


@pytest.mark.parametrize(
    ("tools_source", "expected"),
    [
        (
            TOOLS.replace("name=BETA_NAME", "name=build_name()"),
            "tool name cannot be resolved statically for factory: create_beta_tool",
        ),
        (
            TOOLS.replace("def create_beta_tool", "def other_factory"),
            "tool name cannot be resolved statically for factory: create_beta_tool",
        ),
        (
            TOOLS.replace('"alpha_tool"', '"beta_tool"'),
            "tool name is registered more than once: beta_tool",
        ),
        (
            TOOLS.replace('"alpha_tool"', '"alpha-tool"'),
            "tool name is not a plain identifier for factory: create_alpha_tool",
        ),
        (
            "def broken(:\n",
            "tool name cannot be resolved statically for factory: create_alpha_tool",
        ),
    ],
)
def test_unresolvable_or_invalid_tool_names_are_reported(
    tmp_path: Path, tools_source: str, expected: str
) -> None:
    root = _repository(tmp_path)
    _write(root / "src/proofcoder/tools/sample.py", tools_source)

    assert expected in _failures(root, "documentation.tools")


def test_missing_tool_module_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "src/proofcoder/tools/sample.py").unlink()

    assert _failures(root, "documentation.tools") == [
        "tool name cannot be resolved statically for factory: create_alpha_tool",
        "tool name cannot be resolved statically for factory: create_beta_tool",
    ]


# ---------- ADR records and index ----------


def test_adr_record_problems_are_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    adr = root / "docs/adr"
    _write(adr / "0001-duplicate-number.md", _adr("0001"))
    _write(adr / "0002-bad-status.md", _adr("0002", status="draft"))
    _write(adr / "0003-wrong-title.md", _adr("0003", title_number="0004"))
    _write(adr / "0004-Bad_Name.md", _adr("0004"))
    _write(adr / "0005-no-status.md", f"# ADR-0005{COLON}Decision\n\nNo header fields.\n")
    _write(adr / "0006-superseded.md", _adr("0006", status="已被 ADR-0001 取代"))
    _write(adr / "notes.md", "# Notes\n")

    records = _checks(root, "documentation.adr_status")
    found = [(check.path, check.message) for check in records]

    assert all(check.status is CheckStatus.FAIL for check in records)
    assert found == [
        (
            "docs/adr/0001-first-decision.md",
            "ADR number is used by more than one file: 0001",
        ),
        ("docs/adr/0002-bad-status.md", "ADR status is not one of the allowed values"),
        (
            "docs/adr/0003-wrong-title.md",
            "ADR title must start with '# ADR-NNNN' and use the number from its file name",
        ),
        (
            "docs/adr/0004-Bad_Name.md",
            "ADR file name must be NNNN- followed by lowercase words joined by hyphens",
        ),
        (
            "docs/adr/0005-no-status.md",
            f"ADR has no '- 状态{COLON}' line in its header",
        ),
        (
            "docs/adr/notes.md",
            "ADR directory contains a file that is not an NNNN-title.md record",
        ),
    ]


def test_adr_index_problems_are_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    adr = root / "docs/adr"
    _write(adr / "0002-second-decision.md", _adr("0002"))
    _write(adr / "0003-third-decision.md", _adr("0003", status="提议"))
    _write(adr / "0004-fourth-decision.md", _adr("0004", status="已拒绝"))
    _write(
        adr / "README.md",
        _adr_index(
            (
                _index_row("0001", "0001-first-decision.md"),
                _index_row("0001", "0001-first-decision.md"),
                _index_row("0002", "0009-missing.md"),
                "| 0003 | Decision | 提议 | 2026-01-01 |",
                _index_row("0004", "0004-fourth-decision.md", status="已接受"),
            )
        ),
    )

    assert _failures(root, "documentation.adr_index") == [
        "ADR index lists the same record more than once: 0001",
        "ADR index row does not link to a valid record file: 0002",
        "ADR index row must start with a [NNNN](file.md) link and include a status",
        "ADR index status differs from the record's status: 0004",
        "ADR is missing from the index: 0003",
    ]


def test_missing_adr_index_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "docs/adr/README.md").unlink()

    assert _failures(root, "documentation.adr_index") == [f"{MISSING_PREFIX}docs/adr/README.md"]
    assert _failures(root, "documentation.adr_status") == []


def test_missing_adr_directory_fails_both_adr_checks(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    shutil.rmtree(root / "docs/adr")
    expected = ["ADR directory is missing or not an ordinary directory: docs/adr"]

    assert _failures(root, "documentation.adr_status") == expected
    assert _failures(root, "documentation.adr_index") == expected


def test_symlinked_adr_directory_is_not_trusted(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repository")
    outside = tmp_path / "outside"
    shutil.move(str(root / "docs/adr"), str(outside))
    try:
        (root / "docs/adr").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    assert _failures(root, "documentation.adr_status") == [
        "ADR directory is missing or not an ordinary directory: docs/adr"
    ]


# ---------- roadmap ----------


def test_roadmap_status_problems_are_reported_with_lines(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(
        root / "docs/ROADMAP.md",
        "| 小项 | 状态 | PR |\n| --- | --- | --- |\n"
        "| 1 | done | [#1](https://example.invalid/1) |\n"
        f"| 2 | 已完成 | {EM_DASH} |\n"
        "| 3 | 暂缓 |\n"
        f"| 4 | 阻塞 until review | {EM_DASH} |\n",
    )

    found = [(check.line, check.message) for check in _checks(root, "documentation.roadmap_status")]

    assert found == [
        (3, "roadmap status is not one of the allowed values"),
        (4, "completed roadmap item has no pull request reference"),
        (5, "roadmap table row has a different number of cells than its header"),
    ]


def test_roadmap_without_status_table_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(root / "docs/ROADMAP.md", "| 问题 | 影响 |\n| --- | --- |\n| a | b |\n")

    assert _failures(root, "documentation.roadmap_status") == [
        "roadmap has no table with a status column"
    ]


def test_missing_roadmap_is_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "docs/ROADMAP.md").unlink()

    assert _failures(root, "documentation.roadmap_status") == [f"{MISSING_PREFIX}docs/ROADMAP.md"]


# ---------- specification layout ----------


def test_layout_paths_that_do_not_exist_are_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    layout = LAYOUT + "\n+-- tests/\n|   \\-- integration/\n\\-- README.md/"
    _write(root / "docs/DEVELOPMENT_SPEC.md", _specification(layout=layout))

    records = _checks(root, "documentation.spec_layout")

    assert [check.message for check in records] == [
        f"{LAYOUT_MISSING_PREFIX}tests/",
        f"{LAYOUT_MISSING_PREFIX}tests/integration/",
        f"{LAYOUT_MISSING_PREFIX}README.md/",
    ]
    assert all(check.line is not None for check in records)


def test_layout_entries_that_cannot_be_trusted_are_reported(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    layout = (
        "proofcoder/\n+-- README.md\n|   \\-- nested.md\n+-- ../escape\n"
        "not a tree line\n\n\\-- docs/"
    )
    _write(root / "docs/DEVELOPMENT_SPEC.md", _specification(layout=layout))

    assert _failures(root, "documentation.spec_layout") == [
        "specification layout entry is nested under a file or skips a level",
        "specification layout entry is not a plain relative path",
        "specification layout line cannot be parsed",
    ]


@pytest.mark.parametrize(
    "specification",
    [
        "# Specification\n\n## 7. Tools\n",
        "# Specification\n\n### 5.1 Layout\n\n### 5.2 Next\n\n```text\nproofcoder/\n```\n",
        "# Specification\n\n### 5.1 Layout\n\n```text\nproofcoder/\n+-- README.md\n",
        "# Specification\n\n### 5.1 Layout\n\n```text\n```\n",
        "# Specification\n\n### 5.1 Layout\n\n```text",
    ],
)
def test_missing_or_unterminated_layout_tree_is_reported(
    tmp_path: Path, specification: str
) -> None:
    root = _repository(tmp_path)
    _write(root / "docs/DEVELOPMENT_SPEC.md", _tree(specification))

    assert _failures(root, "documentation.spec_layout") == [
        "specification section 5.1 has no text directory tree"
    ]


def test_missing_specification_is_reported_for_layout(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "docs/DEVELOPMENT_SPEC.md").unlink()

    assert _failures(root, "documentation.spec_layout") == [
        f"{MISSING_PREFIX}docs/DEVELOPMENT_SPEC.md"
    ]


# ---------- output safety ----------


def test_failure_messages_never_echo_document_text(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(root / "docs/adr/0002-leaky-status.md", _adr("0002", status=SENSITIVE_SENTINEL))
    _write(
        root / "docs/ROADMAP.md",
        f"| 小项 | 状态 |\n| --- | --- |\n| 1 | {SENSITIVE_SENTINEL} |\n",
    )
    _write(
        root / "docs/DEVELOPMENT_SPEC.md",
        _specification(layout=f"proofcoder/\n{SENSITIVE_SENTINEL}"),
    )
    _write(root / "README.md", _readme(rows=("alpha_tool", "beta_tool", f"{SENSITIVE_SENTINEL} x")))

    rendered = format_json(ComplianceReport(check_documentation_consistency(root)))

    assert SENSITIVE_SENTINEL not in rendered
    assert json.loads(rendered)["automatic_pass"] is False
