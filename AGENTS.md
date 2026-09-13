# ProofCoder Repository Instructions

## Source of truth

- Before modifying the project, read `docs/DEVELOPMENT_SPEC.md` completely. It is authoritative for scope, architecture, protocols, security boundaries, stages, and acceptance criteria.
- Read `docs/ROADMAP.md` to find the current stage and item. Work on that item unless the user asks for something else.
- Read the architecture decision records in `docs/adr/` that the current item or the affected specification sections reference.
- Do not modify `docs/DEVELOPMENT_SPEC.md` unless the user explicitly requests it or approves the stage ADR that requires the change.
- If a requested change conflicts with the specification or project redlines, stop and explain the conflict before editing.

## Ownership scope

- This is a single-author repository with no other contributors. No file in this codebase is owned by another teammate, including core entry points, main logic, routing files, and primary configuration files.
- This clarification covers ownership only and does not expand task scope. Preserve existing worktree changes, and treat per-task file scope as defined by what the current prompt explicitly states may be inspected, implemented, or affected — not by the agent's own judgment of what is "reasonably required."

## Project redlines

- Do not use any agent framework or agent SDK, including LangChain, LlamaIndex, OpenAI Agents SDK, Claude Agent SDK, AutoGen, or CrewAI.
- Do not depend on API-hosted code execution, file access, sandbox, Code Interpreter, or Files API capabilities.
- Model-provider client libraries may only handle API communication and native tool-calling protocol objects.
- Conversation history, context management, tool definitions, local tool execution, output parsing, retry policy, termination conditions, and error handling must be implemented in this repository.
- All file and command tools must execute locally inside the user-selected workspace.
- Never read, print, log, persist, or commit API keys. Credentials must come from environment variables.

## Change workflow

Specification section 18 defines the role of each document. Apply it on every change:

1. Identify the roadmap item. Use one branch and one pull request per item, and never commit directly to `main`.
2. If the item changes scope, non-goals, tool or command constraints, or any security boundary, first write an ADR from `docs/adr/0000-template.md` together with the matching specification change, and get user approval before implementing.
3. Implement the item with offline tests. Add or update evaluation fixtures when the stage exit criteria require them.
4. In the same change, update every document the change affects: `README.md`, `docs/DESIGN.md`, `docs/THREAT_MODEL.md`, `docs/COMPLIANCE.md`, `docs/EVAL_REPORT.md`, `CHANGELOG.md`, and the item status in `docs/ROADMAP.md`.
5. Factual documents describe only merged behavior. Plans belong in the specification's stage sections, the roadmap, or proposed ADRs.
6. Run the local checks, then open a pull request that completes `.github/pull_request_template.md`.

## Engineering rules

- Target Python 3.11 or newer.
- Use the `src` layout, explicit type annotations, and dataclasses for internal state and protocol models.
- Follow the implementation stages and exit criteria in `docs/DEVELOPMENT_SPEC.md`.
- Work on one bounded milestone at a time; do not silently implement later stages.
- Keep the core synchronous unless the specification is deliberately revised.
- Default tests must run offline with deterministic fake or scripted model clients.
- Real API tests must be explicit and opt-in.
- Convert recoverable failures into structured errors; do not silently swallow exceptions.
- Preserve existing user changes and avoid destructive Git operations.
- Do not create commits or push changes unless the user explicitly requests it.
- Before claiming completion, run the relevant tests and static checks and report their exact commands and results.

## Local checks

Run the commands in the README's "Development and Verification" section before pushing. In addition:

- The secret scanner's `history` scope requires Git 2.44 or newer. With older Git it fails with `GIT_OUTPUT_ERROR`, and `tests/unit/test_secret_scan.py` then fails before it reports findings, which hides real findings. On older Git, stage the change and require this command to pass:
  `uv run --offline python scripts/secret_scan.py --scope working-tree --scope index --format json`
- In tests, name sentinel values the way `tests/unit/test_cli.py` does (`SENSITIVE_SENTINEL = "never-..."`). Do not assign literal values to names that look like credentials, such as names ending in `KEY`, `TOKEN`, or `SECRET`.
