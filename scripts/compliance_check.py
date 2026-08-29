"""Command-line entry point for ProofCoder's offline compliance checks."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from proofcoder.compliance import (
    ComplianceInfrastructureError,
    format_json,
    format_text,
    infrastructure_error_json,
    run_compliance,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the small standard-library command-line parser."""

    parser = argparse.ArgumentParser(
        prog="compliance_check",
        description="Run deterministic offline ProofCoder repository compliance checks.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to inspect (default: current directory)",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker and return 0, 1, or 2 without exposing tracebacks."""

    args = build_parser().parse_args(argv)
    try:
        report = run_compliance(args.root)
    except ComplianceInfrastructureError as error:
        if args.format == "json":
            print(infrastructure_error_json(error))
        else:
            print(f"ERROR {error.code}: {error}")
        return 2

    print(format_json(report) if args.format == "json" else format_text(report))
    return 0 if report.automatic_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
