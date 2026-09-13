# Changelog

All notable user-facing changes to ProofCoder are recorded in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Version numbers will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) from the first tagged release.

No version has been tagged yet. Planned versions and stages are tracked in the [roadmap](docs/ROADMAP.md).

## [Unreleased]

### Added

- `proofcoder serve`: a local browser interface that runs the same bounded agent loop as `run`, with a workspace picker, live event rendering, run history replay, an environment self-check, and a stop button ([ADR-0002](docs/adr/0002-local-browser-interface.md)).
- Cooperative cancellation for the agent loop, used by the browser stop button. A cancelled run ends as `interrupted`.
- Project governance documents: development specification v3.0, the [roadmap](docs/ROADMAP.md), [architecture decision records](docs/adr/README.md), this changelog, `CLAUDE.md`, and a pull request template ([ADR-0001](docs/adr/0001-document-governance.md)).

### Changed

- The development specification's scope and non-goals were revised for the v3.0 roadmap ([ADR-0003](docs/adr/0003-v3-scope-revision.md)).

## Baseline before this changelog

The `0.1.0` version in `pyproject.toml` covers development stages A–E: the DeepSeek client, the repository-owned agent loop, the seven local tools, the default-deny command policy, evidence-gated completion, sanitized JSONL traces, the `doctor`, `run`, `trace`, and `eval` commands, and offline cross-platform CI with compliance and secret scanning.
