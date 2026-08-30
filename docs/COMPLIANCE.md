# ProofCoder Compliance Evidence

## 1. Purpose and Scope

This document records the compliance evidence for ProofCoder at commit `f0dd69e`
(`fix: harden ripgrep search execution`) on branch `main`. The initial review state
was clean. This document is the only intended working-tree addition made by the
review, so the implementation conclusion is bound to that commit and the inspected
state rather than to future revisions.

The review compares the repository with the architecture, security boundaries, and
acceptance criteria in `docs/DEVELOPMENT_SPEC.md`. It covers dependency manifests,
local capability ownership, Python imports and subprocess starts, provider request
construction, command execution, accelerated text search, and the repository's
offline secret scanner. It does not perform a real provider request, use a network,
or read credential files.

## 2. Compliance Conclusion

For the reviewed commit and state, the mechanical checker reports 18 passes, zero
failures, and four items requiring manual review. All four dynamic sinks were traced
from input to execution and are **reviewed and acceptable** within the documented
boundaries below. The secret scan completed across the working tree, index, and all
reachable Git history with zero findings and zero errors.

No reviewed dependency or import is an agent framework or agent SDK. No reviewed
subprocess start launches an existing agent product. Provider integration uses an
ordinary API client for communication and native function-tool payloads; ProofCoder
owns conversation history, context selection, tool definitions and validation, local
tool execution, retry policy, stopping rules, structured errors, tracing, and
evaluation logic.

This is an evidence-based conclusion, not a claim of absolute security or proof that
future dependency, environment, configuration, or source changes remain compliant.

## 3. Official Rule-to-Evidence Matrix

| Rule from `docs/DEVELOPMENT_SPEC.md` | Implementation evidence | Test or checker evidence | Review conclusion |
| --- | --- | --- | --- |
| Do not use an agent framework or agent SDK. | `pyproject.toml`, `uv.lock`, and imports under `src/proofcoder/` contain no listed agent framework or SDK. | `dependency.*`, `python.imports`, and `python.subprocesses` checks in `src/proofcoder/compliance.py`; `tests/unit/test_compliance.py`. | Satisfied for the reviewed dependency and source set. |
| Do not delegate execution to an existing coding-agent product. | `proofcoder.agent.AgentLoop` is the local control loop. `proofcoder.safety.commands.prepare_command` allowlists supported command families; unknown or dangerous targets are rejected. | `tests/unit/test_agent.py`, `tests/unit/test_agent_d2.py`, `tests/unit/test_command_policy.py`; mechanical inspection of ten subprocess calls. | Satisfied for reviewed subprocess starts. |
| Do not depend on hosted code execution, hosted file access, sandboxes, Code Interpreter, or Files API. | `proofcoder.llm.deepseek.DeepSeekClient.complete` sends messages and native function schemas only. File, search, edit, and command tools execute in the selected local workspace. | `python.hosted_tools`; `tests/unit/test_deepseek.py`, `tests/unit/test_tools.py`, `tests/unit/test_read_file.py`, `tests/unit/test_search_text.py`, `tests/unit/test_edit_tools.py`, `tests/unit/test_run_command.py`. | Satisfied; no hosted file/search/execution declaration was found. |
| Provider libraries may handle only API transport and native tool-calling objects. | `proofcoder.llm.deepseek.DeepSeekClient` normalizes provider responses into repository protocol dataclasses. `proofcoder.tools.base.ToolDefinition.to_openai_schema` produces native function schemas. | `tests/unit/test_deepseek.py`, especially request-contract, tool-call preservation, retry-disable, and sanitized-error tests. | Satisfied within the provider client's reviewed request surface. |
| The repository must own history, context, tools, execution, retries, termination, and errors. | `proofcoder.context.MessageHistory`, `proofcoder.context.ContextManager`, `proofcoder.tools.registry.ToolRegistry`, `proofcoder.agent.AgentLoop`, `proofcoder.retry.retry_delay_seconds`, `proofcoder.progress.ProgressTracker`, and structured result/error dataclasses implement these concerns locally. | Capability checks plus `tests/unit/test_context.py`, `tests/unit/test_context_manager.py`, `tests/unit/test_tools.py`, `tests/unit/test_retry.py`, `tests/unit/test_progress.py`, and agent tests. | Satisfied for the capabilities represented by the implementation and tests. |
| File and command tools must run locally and remain workspace-scoped. | `proofcoder.safety.paths` resolves and bounds workspace paths. `proofcoder.tools.command.create_run_command_tool` uses locally prepared argv and workspace cwd. `proofcoder.tools.search.create_search_text_tool` searches local files. | File/edit/search/command tests, including path escape, symlink, argv, environment, timeout, and output-limit cases. | Satisfied at the application-policy layer; this is not an OS sandbox. |
| Provider credentials must enter local configuration from environment variables. Secret values must not be exposed to the model, model-callable tools, or tool subprocesses; written to logs, traces, or errors; committed to the repository or Git history; or otherwise persisted. Protected credential files such as `.env` must not be readable through project file tools. | `proofcoder.llm.deepseek.DeepSeekConfig` names the provider key variable used by local configuration. `proofcoder.safety.secrets` filters tool subprocess environments and redacts output, while `proofcoder.safety.paths` blocks project file-tool access to protected credential paths. The secret scanner reports metadata rather than secret contents. | `tests/unit/test_deepseek.py`, `tests/unit/test_run_command.py`, `tests/unit/test_secret_scan.py`; three-scope secret scan completed with no findings. | Satisfied by reviewed code and scan evidence, subject to pattern-scanner limitations. |

## 4. Locally Implemented Capability Matrix

| Capability | Local implementation | Representative deterministic tests | Evidence status |
| --- | --- | --- | --- |
| Agent loop | `proofcoder.agent.AgentLoop` | `tests/unit/test_agent.py`, `tests/unit/test_agent_d2.py` | Mechanical `PASS` |
| API retry policy | `proofcoder.retry.retry_delay_seconds`; `proofcoder.agent.AgentLoop._request_model` | `tests/unit/test_retry.py`, `tests/unit/test_agent_d2.py` | Mechanical `PASS` |
| Command policy and execution | `proofcoder.safety.commands.prepare_command`; `proofcoder.tools.command.create_run_command_tool` | `tests/unit/test_command_policy.py`, `tests/unit/test_run_command.py` | Mechanical `PASS`; dynamic starts manually reviewed |
| DeepSeek client | `proofcoder.llm.deepseek.DeepSeekClient` | `tests/unit/test_deepseek.py` | Mechanical `PASS`; dynamic request manually reviewed |
| Evaluation pipeline | `proofcoder.eval_fixtures`, `proofcoder.eval_core`, `proofcoder.eval_runner` | `tests/unit/test_eval_fixtures.py`, `tests/unit/test_eval_core.py`, `tests/unit/test_eval_runner.py` | Mechanical `PASS` |
| Local file tools | `proofcoder.tools.files`, `proofcoder.tools.search`, `proofcoder.tools.edit` | `tests/unit/test_read_file.py`, `tests/unit/test_search_text.py`, `tests/unit/test_edit_tools.py` | Mechanical `PASS`; ripgrep start manually reviewed |
| History and context | `proofcoder.context.MessageHistory`; `proofcoder.context.ContextManager` | `tests/unit/test_context.py`, `tests/unit/test_context_manager.py` | Mechanical `PASS` |
| No-progress termination | `proofcoder.progress.ProgressTracker`; `proofcoder.agent.AgentLoop` | `tests/unit/test_progress.py`, `tests/unit/test_agent_d2.py` | Mechanical `PASS` |
| Tool registry and validation | `proofcoder.tools.registry.ToolRegistry` | `tests/unit/test_tools.py` | Mechanical `PASS` |
| Trace and replay | `proofcoder.events`, `proofcoder.trace` | `tests/unit/test_events.py`, `tests/unit/test_trace.py` | Mechanical `PASS` |
| Verification and finish | `proofcoder.verification.VerificationTracker`; `proofcoder.tools.finish.build_finish_outcome` | `tests/unit/test_verification.py`, `tests/unit/test_finish_task.py` | Mechanical `PASS` |

The capability checker verifies the presence of named local symbols and their mapped
test files. Manual source review is still required to understand behavior; capability
presence alone is not a security proof.

## 5. Dependency Review

`pyproject.toml` declares two runtime dependencies: `openai` for provider API
communication and `rich` for terminal presentation. Its development extra contains
`pytest`, `pytest-cov`, and `ruff`. `uv.lock` is a complete Python 3.11-or-newer lock
with 30 package records, including the editable `proofcoder` package and transitive
transport, validation, rendering, test, coverage, and lint dependencies.

The mechanical review checked zero dependency-group entries, three optional
development entries, two runtime entries, and all 30 locked package names against
the forbidden distribution list. Every dependency check passed. No LangChain,
LlamaIndex, OpenAI Agents SDK, Claude Agent SDK, AutoGen, CrewAI, or other listed
agent framework/SDK distribution was found. Package-name screening does not replace
source, provenance, vulnerability, or future-version review.

## 6. Mechanical Compliance Results

The offline command below was run from the repository root against commit `f0dd69e`:

```powershell
.\.venv\Scripts\uv.exe run python scripts/compliance_check.py --format json
```

Result:

| Metric | Value |
| --- | ---: |
| `automatic_pass` | `true` |
| `pass` | 18 |
| `fail` | 0 |
| `review` | 4 |
| Python files inspected for forbidden imports | 40 |
| Subprocess calls inspected by the static check | 10 |

The four `review` results are intentionally not rewritten as mechanical passes.
Their manual dispositions and remaining limitations are recorded in the next
section. The checker itself states that mechanical checks cannot replace dependency
and call-chain review.

The current full acceptance rerun collected 684 tests: 682 passed and two
platform-gated tests were skipped on Windows. Required coverage was met with 90.09%
total coverage. This current rerun is distinguished from the 90.07% remediation
history value recorded in Section 9.

## 7. Manual Review of Dynamic Calls

| Check ID | Dynamic sink | Input source | Validation / control | Test evidence | Mechanical status | Manual conclusion | Remaining limitation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `python.hosted_tool.dynamic_request` | `proofcoder.llm.deepseek.DeepSeekClient.complete` calls `chat.completions.create(**request)` | A locally constructed top-level mapping; messages come from `ContextManager`; tools come from `ToolRegistry.schemas()` | The request has a fixed set of top-level fields. Tools are native `type: function` schemas produced by `ToolDefinition.to_openai_schema`. No hosted file, search, or execution field is added. The OpenAI client is configured with `max_retries=0`; `AgentLoop._request_model` owns bounded local retry. | `test_openai_client_is_constructed_with_retries_disabled`, `test_request_contract_and_response_normalization`, `test_tools_request_and_multiple_tool_calls_are_preserved`, transient/permanent retry tests in `tests/unit/test_agent_d2.py` | REVIEW | reviewed and acceptable | Correctness still depends on the configured provider endpoint honoring the ordinary Chat Completions/native function-tool contract. |
| `python.subprocess.dynamic` | Main `subprocess.Popen` in `proofcoder.tools.command.create_run_command_tool` | Model-provided `argv`, optional relative `cwd`, and timeout enter through the tool schema and `ToolRegistry`; `AgentLoop` preflights the entire batch | `prepare_command` enforces argv-only input, workspace cwd and path rules, executable resolution, command/module/script allowlists, secret checks, a minimal noninteractive environment, and bounded timeout. Execution uses a list argv, resolved executable, `shell=False`, workspace cwd, no stdin, separate pipes, output caps, and process-group cleanup. | `test_tool_protocol_is_argv_only_and_describes_residual_risk`, command-policy rejection tests, `test_popen_receives_argv_shell_false_no_stdin_and_filtered_environment`, timeout/output/redaction/audit tests, and agent batch-preflight tests | REVIEW | reviewed and acceptable | Allowlisted workspace Python scripts can execute arbitrary code with the user's OS permissions. The policy is an application boundary, not an OS sandbox. |
| `python.subprocess.dynamic` | Windows cleanup `subprocess.run` in `proofcoder.tools.command._terminate_windows_process_tree` | The executable is constructed from local `SYSTEMROOT`/`WINDIR`; the PID is `Popen.pid` from the already-started child | Cleanup is reached only for timeout, interruption, or exceptional cleanup. It requires a regular local `System32/taskkill.exe` and uses fixed arguments `taskkill.exe /PID <pid> /T /F`, `shell=False`, no stdin, discarded output, minimal environment, and a short timeout. POSIX uses process-group signals instead. Failure falls back to direct child termination and reap. | `test_timeout_returns_output_and_terminates_direct_process_without_raw_temporary_files`, `test_timeout_best_effort_cleans_child_process_tree`, and `test_base_exceptions_propagate_after_cleanup`; command policy separately rejects model-requested `taskkill` | REVIEW | reviewed and acceptable | There is no standalone unit test asserting the exact Windows cleanup argv. The exact argv and PID provenance were manually inspected; integration tests cover best-effort timeout/tree cleanup. |
| `python.subprocess.dynamic` | Ripgrep `subprocess.Popen` in `proofcoder.tools.search._search_with_ripgrep` | Candidate resolution starts from operator-supplied `PATH`; the search query, path, glob, mode, and result limit come from the validated local tool call | Only non-empty absolute external `PATH` directories are considered. Relative, dot, workspace, non-file, missing, and invalid final targets are rejected. Windows accepts only `rg.exe`, not script wrappers; POSIX requires an executable regular file. The final canonical target must remain external. Execution uses canonical absolute argv, `shell=False`, workspace cwd, no config, minimal environment, a five-second total deadline, concurrent bounded stdout/stderr capture (2 MiB / 64 KiB), cleanup and reap, no raw backend detail, and safe Python fallback. | `test_ripgrep_uses_argv_no_shell_no_config_and_filtered_environment`, malicious workspace/PATH and injected-resolver tests, symlink tests, Windows case test, POSIX execute-bit test, timeout/reap tests, independent stream-limit tests, failure sanitization, fallback, and semantic-parity tests in `tests/unit/test_search_text.py` | REVIEW | reviewed and acceptable | External ripgrep is trusted through operator `PATH`; no signature or package provenance verification is performed. The reviewed acceptance run exercises Windows behavior, while the POSIX-only execute-bit test is skipped on Windows. |

### Provider request call chain

`AgentLoop.run` obtains native schemas from `ToolRegistry.schemas`, asks
`ContextManager.build` for a bounded message view, and calls
`AgentLoop._request_model`. That method applies the repository retry policy and calls
`DeepSeekClient.complete`. The provider client creates the request mapping locally,
adds the already-local native schemas only when present, and passes it to the ordinary
Chat Completions method. Provider-library retries are disabled, so retry attempts,
delays, counters, time limits, and termination remain repository-owned.

### Main command call chain

Model JSON is parsed and shape-checked by `ToolRegistry.prepare`. The run-command
preflight and executor both use the same preparation path, which calls
`proofcoder.safety.commands.prepare_command`. The resulting immutable prepared
command carries execution argv, display argv, workspace cwd, timeout, command kind,
and filtered environment into `subprocess.Popen`. `AgentLoop` preflights every call in
a multi-tool batch before starting any side effect.

### Windows cleanup call chain

The Windows `taskkill` start is not model-selectable. It is a cleanup fallback fed by
the local child handle's PID. On POSIX, the corresponding cleanup sends `SIGTERM` and,
if needed, `SIGKILL` to the process group. Cleanup exceptions are suppressed only so
that direct child terminate/kill and reap can still be attempted.

### Accelerated search call chain

`RipgrepResolver` returns only a candidate; `_validate_ripgrep_candidate` independently
checks operator `PATH` membership, lexical and final workspace exclusion, platform
filename/type rules, and canonical resolution. The external process is an optional
acceleration path. Startup, nonzero exit, malformed JSON, timeout, or capture overflow
causes bounded cleanup and deterministic local Python fallback without including raw
ripgrep output in the tool result.

## 8. Secret-Scanning Evidence

The repository-owned scanner was run offline with:

```powershell
.\.venv\Scripts\uv.exe run python scripts/secret_scan.py --format json
```

The pre-document baseline scan completed successfully and reported:

| Scope | Candidates | Scanned | Skipped binary | Findings |
| --- | ---: | ---: | ---: | ---: |
| Working tree (Git-visible tracked and untracked, excluding ignored files) | 84 | 84 | 0 | 0 |
| Index | 84 | 84 | 0 | 0 |
| All reachable history (`--all`) | 153 | 153 | 0 | 0 |
| Total | 321 | 321 | 0 | 0 |

The result had `scan_complete: true`, `automatic_pass: true`, zero errors, and zero
warnings. A final acceptance scan also includes this untracked documentation file,
so candidate totals can increase without changing the required conclusion: the scan
must remain complete with zero findings and zero errors.

`proofcoder.secret_scan.scan_repository` covers working-tree content, staged/index
blobs, and every reachable Git object selected by the scanner. It applies bounded
Git-output and blob-size handling, reports path-sensitive names without printing file
contents, emits finding metadata rather than matched secret values, and treats scope
or decoding failures as incomplete scan errors. Pattern-based scanning cannot prove
that every possible secret representation is absent.

## 9. Security Finding and Remediation History

During the preceding manual compliance review, the optional ripgrep acceleration path
was found to rely on `shutil.which("rg")`, without a total execution timeout and with
unbounded `capture_output`. Documentation generation was stopped while that call chain
was hardened. This history describes a security-boundary finding; it does not claim a
demonstrated exploit.

Commit `f0dd69e` replaced that behavior with explicit trusted-candidate validation,
canonical absolute execution, workspace exclusion, Windows wrapper rejection, a total
deadline, independent bounded concurrent stream capture, spawned child-process cleanup
and reap, sanitized fallback, and expanded regression coverage in
`tests/unit/test_search_text.py`.

The recorded Windows remediation validation was 42 search tests passed with one
POSIX-only skip; the full suite was 682 passed with two skips and 90.07% total
coverage. The compliance result was 18 passes, zero failures, and four manual-review
items. The secret scan was complete with zero findings and zero errors. These results
are tied to the remediation commit and its inspected environment, not to later source
or dependency changes.

## 10. Security Boundaries and Limitations

- ProofCoder's command policy constrains model-selected commands but does not provide
  kernel isolation. Allowed workspace Python scripts execute with the current user's
  OS permissions.
- Real model use necessarily contacts the configured API endpoint. This review made
  no network request and performed no real-model evaluation.
- External ripgrep provenance is delegated to the operator-controlled absolute
  `PATH`. The resolver validates location, type, platform name, and workspace
  exclusion but does not verify signatures or package-manager provenance.
- The hardened search acceptance evidence is Windows-targeted. The POSIX-only
  executable-bit branch remains covered by a platform-gated test that is skipped on
  Windows and should be run on a POSIX CI host.
- Windows process-tree cleanup has integration coverage, but no dedicated test
  asserts the exact local `taskkill.exe` argv.
- Static dependency/import/subprocess checks are pattern- and AST-based. Dynamic
  behavior and future code changes still require manual review.
- Secret scanning is pattern-based and bounded. Encoded, fragmented, novel, ignored,
  unreachable, externally stored, or otherwise unrecognized material may fall
  outside its evidence.
- The conclusion assumes the reviewed source, lock file, selected workspace, Git
  object database, environment configuration, and external executables have not been
  replaced after validation.

## 11. Reproduction Commands

Run from the repository root in PowerShell. These commands do not actively send a
request to a provider or model API. In an environment whose dependencies are already
synchronized, the project checks can execute without network access. Enforcing uv's
`--offline` mode requires the local environment and cache to contain every required
dependency; when dependencies or cache entries are missing, ordinary `uv run` may
attempt to access a package source.

```powershell
git status -sb
git log -5 --oneline --decorate

.\.venv\Scripts\uv.exe lock --check
.\.venv\Scripts\uv.exe run ruff format --check .
.\.venv\Scripts\uv.exe run ruff check .
.\.venv\Scripts\uv.exe run pytest tests/unit/test_compliance.py tests/unit/test_search_text.py tests/unit/test_secret_scan.py -q
.\.venv\Scripts\uv.exe run pytest --cov=proofcoder --cov-report=term-missing
.\.venv\Scripts\uv.exe run --locked proofcoder doctor --offline
.\.venv\Scripts\uv.exe run python scripts/compliance_check.py --format json
.\.venv\Scripts\uv.exe run python scripts/secret_scan.py --format json

git diff --check
git diff -- AGENTS.md docs/DEVELOPMENT_SPEC.md docs/EVAL_REPORT.md pyproject.toml uv.lock
git status --short --untracked-files=all
```

Expected acceptance: lock, format, lint, focused tests, full tests/coverage, and offline
doctor all succeed; compliance remains 18 pass / 0 fail / 4 review; secret scanning is
complete with zero findings and zero errors; the protected-file diff prints nothing;
and final short status contains only `?? docs/COMPLIANCE.md`.
