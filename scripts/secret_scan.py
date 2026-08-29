"""Command-line entry point for deterministic offline repository secret scans."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from proofcoder.secret_scan import (
    DEFAULT_SCOPES,
    ScanScope,
    format_json,
    format_text,
    infrastructure_error_report,
    scan_repository,
)

_CLI_SCOPES = {
    "working-tree": ScanScope.WORKING_TREE,
    "index": ScanScope.INDEX,
    "history": ScanScope.HISTORY,
}


def build_parser() -> argparse.ArgumentParser:
    """Build the standard-library argument parser."""

    parser = argparse.ArgumentParser(
        prog="secret_scan",
        description="Scan Git repository evidence surfaces for sensitive information offline.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Git repository root to inspect (default: current directory)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--scope",
        choices=tuple(_CLI_SCOPES),
        action="append",
        help="scope to scan; repeat to select more than one (default: all)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scanner and return 0, 1, or 2 without exposing tracebacks."""

    args = build_parser().parse_args(argv)
    scopes = (
        DEFAULT_SCOPES if args.scope is None else tuple(_CLI_SCOPES[item] for item in args.scope)
    )
    try:
        report = scan_repository(args.root, scopes=scopes)
    except Exception:
        report = infrastructure_error_report(
            scopes,
            "INTERNAL_SCAN_ERROR",
            "An internal scanner error prevented a complete report.",
        )
    print(format_json(report) if args.format == "json" else format_text(report), end="")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
