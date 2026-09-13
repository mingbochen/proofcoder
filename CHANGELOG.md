# Changelog

All notable user-facing changes to ProofCoder are recorded in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from the first tagged release.

No version has been tagged yet. Planned versions and stages are tracked in the [roadmap](docs/ROADMAP.md).

## [Unreleased]

### Added

- Licensed under the [Apache License 2.0](LICENSE).
- `proofcoder serve`: a local browser interface that runs the same bounded agent loop as `run`, with a workspace picker, live event rendering, run history replay, an environment self-check, and a stop button ([ADR-0002](docs/adr/0002-local-browser-interface.md)).
- Cooperative cancellation for the agent loop, used by the browser stop button. A cancelled run ends as `interrupted`.
- Project governance documents: development specification v3.0, the [roadmap](docs/ROADMAP.md), [architecture decision records](docs/adr/README.md), this changelog, `CLAUDE.md`, and a pull request template ([ADR-0001](docs/adr/0001-document-governance.md)).
- Run checkpoints: before its first model call, every run records a content-addressed copy of the workspace baseline under the ignored `.proofcoder/checkpoints/<run_id>` directory, and a `checkpoint` trace event reports what it covers and what it does not. The baseline is captured by scanning rather than by intercepting writes, so a later rollback covers changes made by an allowed workspace script, build, or test as well as those made by the built-in file tools. Sensitive paths such as `.env` contribute neither content nor a content digest and are never written back; oversized files, ignored directories, symbolic links, and anything outside the workspace stay uncovered and are reported. A capture failure ends the run as `checkpoint_error` before the provider is contacted, `proofcoder run --no-checkpoint` starts without one, and the three most recent checkpoints are kept under a total size limit, pruned before each capture and never removing a run trace ([ADR-0004](docs/adr/0004-workspace-checkpoints.md)).
- Rollback planning and application for a recorded checkpoint, with per-path sources, a preview of every action, and a result that names each restored, recreated, deleted, skipped, and failed path. The command-line and browser entry points that call it arrive in later stage F items.
- Documentation consistency checks in `scripts/compliance_check.py`: registered tools must match specification section 7 and the README tool table, ADR records must agree with the ADR index, roadmap status values must be valid, and every path in the specification's directory layout must exist.

### Changed

- The README states that the secret scanner's `history` scope requires Git 2.44 or newer.
- The development specification's scope and non-goals were revised for the v3.0 roadmap ([ADR-0003](docs/adr/0003-v3-scope-revision.md)).
- The development specification defines the Stage F workspace checkpoint and rollback constraints in new sections 10.5 and 13.4: a content-addressed baseline captured before the first model call, rollback that covers changes made by allowed workspace processes as well as the built-in file tools, sensitive paths recorded as metadata only and never restored, and a bounded retention policy ([ADR-0004](docs/adr/0004-workspace-checkpoints.md)). No checkpoint or rollback behavior is implemented yet.

## Baseline before this changelog

The `0.1.0` version in `pyproject.toml` covers development stages A–E: the DeepSeek client, the repository-owned agent loop, the seven local tools, the default-deny command policy, evidence-gated completion, sanitized JSONL traces, the `doctor`, `run`, `trace`, and `eval` commands, and offline cross-platform CI with compliance and secret scanning.
