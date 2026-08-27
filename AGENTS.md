# ProofCoder Repository Instructions

## Source of truth

- Before modifying the project, read `docs/DEVELOPMENT_SPEC.md` completely.
- Treat that document as authoritative for project scope, architecture, protocols, security boundaries, and acceptance criteria.
- Do not modify `docs/DEVELOPMENT_SPEC.md` unless the user explicitly requests it.
- If a requested change conflicts with the specification or project redlines, stop and explain the conflict before editing.

## Project redlines

- Do not use any agent framework or agent SDK, including LangChain, LlamaIndex, OpenAI Agents SDK, Claude Agent SDK, AutoGen, or CrewAI.
- Do not depend on API-hosted code execution, file access, sandbox, Code Interpreter, or Files API capabilities.
- Model-provider client libraries may only handle API communication and native tool-calling protocol objects.
- Conversation history, context management, tool definitions, local tool execution, output parsing, retry policy, termination conditions, and error handling must be implemented in this repository.
- All file and command tools must execute locally inside the user-selected workspace.
- Never read, print, log, persist, or commit API keys. Credentials must come from environment variables.

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