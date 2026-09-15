"""Offline tests for cross-run sessions and the bounded carry they produce.

The load-bearing test here is ``test_session_carry_never_verifies_the_next_run``: it
asserts the completion status rather than a field, so any future change that quietly
restores run state from a session fails it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from proofcoder.agent import AgentLoop
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import (
    CompletionStatus,
    FunctionCall,
    ModelResponse,
    TerminationReason,
    ToolCall,
)
from proofcoder.session import (
    CARRY_BUDGET_RATIO,
    MAX_SESSION_RUNS,
    MAX_WORKSPACE_SESSIONS,
    RunRecord,
    Session,
    SessionError,
    SessionVerification,
    append_run_record,
    build_session_carry,
    create_session,
    delete_session,
    end_session,
    list_sessions,
    load_session,
    prune_workspace_sessions,
    run_record_from_result,
    session_carry_payload,
    session_path,
    validate_session_id,
)
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import create_create_file_tool
from proofcoder.tools.files import create_list_files_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry

SENSITIVE_SENTINEL = "never-carry-this-value-across-a-run"
SESSION_A = "0" * 32
SESSION_B = "1" * 32
RUN_A = "a" * 32
RUN_B = "b" * 32


def _record(
    *,
    run_id: str = RUN_A,
    task: str = "Fix the helper.",
    recorded_at: str = "2026-09-15T00:00:00.000000Z",
    verification: SessionVerification | None = None,
    summary: str | None = "I fixed the helper.",
    limitations: tuple[str, ...] = (),
    blocked_reason: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        task=task,
        recorded_at=recorded_at,
        termination_reason="finish_task",
        completion_status="completed_verified",
        changed_files=("helper.py",),
        verification=verification,
        model_calls=3,
        tool_calls=4,
        summary=summary,
        limitations=limitations,
        blocked_reason=blocked_reason,
    )


def _session(*records: RunRecord, session_id: str = SESSION_A) -> Session:
    return Session(
        session_id=session_id,
        created_at="2026-09-15T00:00:00.000000Z",
        ended_at=None,
        runs=records,
    )


def _response(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        content=None if calls else "done",
        reasoning_content=None,
        finish_reason="tool_calls" if calls else "stop",
        usage=None,
        tool_calls=calls,
    )


def _call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=arguments))


def _registry(workspace: Path) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(create_list_files_tool(workspace))
    registry.register(create_create_file_tool(workspace, checkpoint_available=lambda: True))
    registry.register(create_run_command_tool(workspace, environ={}))
    registry.register(create_finish_task_tool(workspace))
    return registry


def _loop(workspace: Path, client: ScriptedClient, **kwargs: object) -> AgentLoop:
    return AgentLoop(
        client=client,
        registry=_registry(workspace),
        workspace=workspace,
        system_prompt="test system prompt",
        max_steps=6,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        random_value=lambda: 0.0,
        **kwargs,  # type: ignore[arg-type]
    )


def test_session_carry_never_verifies_the_next_run(tmp_path: Path) -> None:
    """The previous run's verification must not survive into this one.

    The model is given a session whose earlier run verified, then immediately requests
    completion after one file change without running any command. If evidence could
    cross a run boundary, this would come back verified.
    """

    verified = _record(
        verification=SessionVerification(argv=("python", "-m", "pytest"), cwd=".", exit_code=0)
    )
    carry = build_session_carry(_session(verified), context_budget_bytes=256 * 1024)
    client = ScriptedClient(
        [
            _response(
                _call(
                    "call-1",
                    "create_file",
                    json.dumps({"path": "helper.py", "content": "value = 2\n"}),
                )
            ),
            _response(
                _call(
                    "call-2",
                    "finish_task",
                    json.dumps(
                        {
                            "summary": "Reused the verification from the earlier run.",
                            "changed_files": ["helper.py"],
                            "verification_command": ["python", "-m", "pytest"],
                        }
                    ),
                )
            ),
        ]
    )

    result = _loop(tmp_path, client, carry=carry).run("Change the helper again.")

    assert result.termination_reason is TerminationReason.FINISH_TASK
    assert result.completion_status is CompletionStatus.COMPLETED_UNVERIFIED
    assert result.verification_command is None
    # The carry did reach the model: the prompt it saw names the earlier run.
    sent = client.requests[0].messages
    assert verified.run_id in sent[1]["content"]


def test_carry_prefixes_the_task_message_and_leaves_the_system_prompt_alone(
    tmp_path: Path,
) -> None:
    carry = build_session_carry(_session(_record()), context_budget_bytes=256 * 1024)
    client = ScriptedClient([_response()])

    result = _loop(tmp_path, client, carry=carry).run("Do the next thing.")

    messages = client.requests[0].messages
    assert messages[0]["role"] == "system"
    assert carry.session_id not in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("[ProofCoder session ")
    assert messages[1]["content"].endswith("Do the next thing.")
    # The task event, and so the trace, still reports only what the user asked for.
    assert result.history.messages[1].content != "Do the next thing."


def test_run_without_a_session_is_unchanged(tmp_path: Path) -> None:
    with_none = ScriptedClient([_response()])
    _loop(tmp_path, with_none).run("Do the thing.")

    messages = with_none.requests[0].messages
    assert messages[1]["content"] == "Do the thing."


def test_session_event_reports_the_carry_and_marks_verification_expired() -> None:
    verified = _record(
        verification=SessionVerification(argv=("pytest", "-q"), cwd=".", exit_code=0)
    )
    carry = build_session_carry(_session(verified), context_budget_bytes=256 * 1024)

    payload = session_carry_payload(carry)

    assert payload["session_id"] == SESSION_A
    assert payload["carried_runs"] == 1
    assert payload["dropped_runs"] == 0
    assert payload["carried_accounts"] == 1
    assert payload["carried_bytes"] == carry.byte_count
    assert payload["limit_bytes"] == int(256 * 1024 * CARRY_BUDGET_RATIO)
    assert payload["prior_verification"] == {
        "argv": ["pytest", "-q"],
        "cwd": ".",
        "exit_code": 0,
        "expired": True,
    }


def test_carry_assembly_is_deterministic() -> None:
    session = _session(_record(run_id=RUN_A), _record(run_id=RUN_B, task="Second task."))

    first = build_session_carry(session, context_budget_bytes=256 * 1024)
    second = build_session_carry(session, context_budget_bytes=256 * 1024)

    assert first.text == second.text
    assert first.carried_runs == 2
    assert first.dropped_runs == 0


def test_carry_drops_the_oldest_account_then_the_oldest_record() -> None:
    """The documented trim order, checked at three budgets on one session.

    Everything fits at the first budget. The second forces one account out, and it must
    be the oldest run's. The third forces the oldest run out entirely -- and the newest
    run keeps its account throughout, because the newest conclusion is the one the next
    run is most likely to need.
    """

    oldest = _record(run_id=RUN_A, task="First task.", summary="First account. " * 8)
    newest = _record(run_id=RUN_B, task="Second task.", summary="Second account. " * 8)
    session = _session(oldest, newest)

    full = build_session_carry(session, context_budget_bytes=256 * 1024)
    assert full.carried_runs == 2
    assert full.carried_accounts == 2

    one_account = build_session_carry(
        session, context_budget_bytes=int((full.byte_count - 8) / CARRY_BUDGET_RATIO)
    )
    assert one_account.carried_runs == 2
    assert one_account.carried_accounts == 1
    assert "First account." not in one_account.text
    assert "Second account." in one_account.text

    one_record = build_session_carry(
        session, context_budget_bytes=int((one_account.byte_count - 8) / CARRY_BUDGET_RATIO)
    )
    assert one_record.carried_runs == 1
    assert one_record.dropped_runs == 1
    assert RUN_A not in one_record.text
    assert RUN_B in one_record.text
    assert "Second account." in one_record.text
    assert session_carry_payload(one_record)["dropped_runs"] == 1


def test_carry_of_an_empty_session_is_empty_but_still_reported() -> None:
    carry = build_session_carry(_session(), context_budget_bytes=256 * 1024)

    assert carry.text == ""
    assert carry.carried_runs == 0
    assert carry.prior_verification is None
    assert session_carry_payload(carry)["carried_bytes"] == 0


def test_a_record_too_large_for_the_budget_carries_nothing() -> None:
    session = _session(_record(summary="x" * 4000))

    carry = build_session_carry(session, context_budget_bytes=64)

    assert carry.text == ""
    assert carry.carried_runs == 0
    assert carry.dropped_runs == 1


@pytest.mark.parametrize("budget", [0, -1])
def test_carry_rejects_a_non_positive_budget(budget: int) -> None:
    with pytest.raises(ValueError):
        build_session_carry(_session(), context_budget_bytes=budget)


def test_sensitive_values_reach_neither_the_stored_file_nor_the_prompt(tmp_path: Path) -> None:
    session = create_session(tmp_path, session_id=SESSION_A)
    client = ScriptedClient(
        [
            _response(
                _call(
                    "call-1",
                    "finish_task",
                    json.dumps({"summary": f"I used {SENSITIVE_SENTINEL} to do it."}),
                )
            )
        ]
    )
    result = _loop(tmp_path, client).run("Do the thing.")

    record = run_record_from_result(
        result, task="Do the thing.", sensitive_values=(SENSITIVE_SENTINEL,)
    )
    append_run_record(tmp_path, session.session_id, record)

    stored = session_path(tmp_path, session.session_id).read_text(encoding="utf-8")
    assert SENSITIVE_SENTINEL not in stored
    carry = build_session_carry(
        load_session(tmp_path, session.session_id), context_budget_bytes=256 * 1024
    )
    assert SENSITIVE_SENTINEL not in carry.text
    assert "I used" in carry.text


def test_session_round_trip_and_lifecycle(tmp_path: Path) -> None:
    session = create_session(tmp_path, session_id=SESSION_A)
    assert session.runs == ()
    assert not session.ended

    append_run_record(tmp_path, SESSION_A, _record(run_id=RUN_A))
    append_run_record(tmp_path, SESSION_A, _record(run_id=RUN_B, task="Second task."))
    reloaded = load_session(tmp_path, SESSION_A)
    assert [record.run_id for record in reloaded.runs] == [RUN_A, RUN_B]

    summaries = list_sessions(tmp_path)
    assert [item.session_id for item in summaries] == [SESSION_A]
    assert summaries[0].run_count == 2

    ended = end_session(tmp_path, SESSION_A)
    assert ended.ended
    # Ending twice is not an error, and it does not move the recorded time.
    assert end_session(tmp_path, SESSION_A).ended_at == ended.ended_at

    with pytest.raises(SessionError) as ended_error:
        append_run_record(tmp_path, SESSION_A, _record())
    assert ended_error.value.code == "SESSION_ENDED"

    delete_session(tmp_path, SESSION_A)
    assert list_sessions(tmp_path) == ()
    with pytest.raises(SessionError) as missing:
        load_session(tmp_path, SESSION_A)
    assert missing.value.code == "SESSION_NOT_FOUND"


def test_stored_runs_stay_within_the_record_bound(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    for index in range(MAX_SESSION_RUNS + 3):
        append_run_record(tmp_path, SESSION_A, _record(run_id=f"{index:032x}"))

    stored = load_session(tmp_path, SESSION_A)

    assert len(stored.runs) == MAX_SESSION_RUNS
    # The oldest records are the ones that go.
    assert stored.runs[-1].run_id == f"{MAX_SESSION_RUNS + 2:032x}"


def test_pruning_keeps_the_session_being_written(tmp_path: Path) -> None:
    for index in range(MAX_WORKSPACE_SESSIONS + 2):
        create_session(
            tmp_path,
            session_id=f"{index:032x}",
            now=f"2026-09-15T00:00:{index:02d}.000000Z",
        )

    oldest = f"{0:032x}"
    remaining = {item.session_id for item in list_sessions(tmp_path)}
    assert len(remaining) <= MAX_WORKSPACE_SESSIONS
    assert oldest not in remaining

    kept = f"{MAX_WORKSPACE_SESSIONS + 1:032x}"
    prune_workspace_sessions(tmp_path, keep_session_id=kept)
    assert kept in {item.session_id for item in list_sessions(tmp_path)}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda document: document.pop("session_id"), "SESSION_INVALID"),
        (lambda document: document.update(schema_version=2), "SESSION_INVALID"),
        (lambda document: document.update(created_at=123), "SESSION_INVALID"),
        (lambda document: document.update(runs={}), "SESSION_INVALID"),
        (lambda document: document.update(unexpected=True), "SESSION_INVALID"),
        (lambda document: document["runs"][0].update(task=""), "SESSION_INVALID"),
        (lambda document: document["runs"][0].update(task="x" * 5000), "SESSION_INVALID"),
        (lambda document: document["runs"][0].update(model_calls=-1), "SESSION_INVALID"),
        (lambda document: document["runs"][0].update(run_id="not-hex"), "SESSION_INVALID"),
        (lambda document: document["runs"][0].update(changed_files="helper.py"), "SESSION_INVALID"),
        (
            lambda document: document["runs"][0].update(limitations=["x"] * 100),
            "SESSION_INVALID",
        ),
        (
            lambda document: document["runs"][0].update(verification={"argv": [], "cwd": "."}),
            "SESSION_INVALID",
        ),
    ],
)
def test_one_bad_field_fails_the_whole_session(
    tmp_path: Path,
    mutate: object,
    code: str,
) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    append_run_record(
        tmp_path,
        SESSION_A,
        _record(verification=SessionVerification(argv=("pytest",), cwd=".", exit_code=0)),
    )
    path = session_path(tmp_path, SESSION_A)
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)  # type: ignore[operator]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_A)

    assert error.value.code == code


def test_a_stored_id_that_disagrees_with_its_directory_is_refused(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    path = session_path(tmp_path, SESSION_A)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["session_id"] = SESSION_B
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_A)

    assert error.value.code == "SESSION_INVALID"


@pytest.mark.parametrize(
    "session_id",
    ["", "../escape", "A" * 32, "0" * 31, "0" * 33, "0123456789abcdef/0123456789abcde"],
)
def test_session_ids_that_are_paths_or_malformed_are_refused(session_id: str) -> None:
    with pytest.raises(SessionError) as error:
        validate_session_id(session_id)

    assert error.value.code == "INVALID_SESSION_ID"


def test_an_oversized_session_file_is_refused_without_parsing(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    path = session_path(tmp_path, SESSION_A)
    path.write_bytes(b"{" + b" " * (256 * 1024))

    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_A)

    assert error.value.code == "SESSION_TOO_LARGE"


def test_run_record_from_result_separates_program_facts_from_model_claims(
    tmp_path: Path,
) -> None:
    client = ScriptedClient(
        [
            _response(
                _call(
                    "call-1",
                    "create_file",
                    json.dumps({"path": "helper.py", "content": "value = 1\n"}),
                )
            ),
            _response(
                _call(
                    "call-2",
                    "finish_task",
                    json.dumps(
                        {
                            "summary": "Wrote the helper.",
                            "changed_files": ["helper.py", "not-really-changed.py"],
                            "limitations": ["No tests were run."],
                        }
                    ),
                )
            ),
        ]
    )
    result = _loop(tmp_path, client).run("Write the helper.")

    record = run_record_from_result(result, task="Write the helper.")

    # Program-recorded: only the file the tool actually wrote, never the model's claim.
    assert record.changed_files == ("helper.py",)
    assert record.completion_status == "completed_unverified"
    assert record.verification is None
    # Model-authored: kept, but in the half that is labeled as claims.
    assert record.summary == "Wrote the helper."
    assert record.limitations == ("No tests were run.",)
    assert record.has_account
    assert not record.without_account().has_account


def test_a_symlinked_session_file_is_refused(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps(_session().to_dict()), encoding="utf-8")
    path = session_path(tmp_path, SESSION_A)
    path.unlink()
    path.symlink_to(real)

    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_A)

    assert error.value.code == "SESSION_NOT_A_FILE"


def test_a_session_that_is_not_json_is_refused(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    session_path(tmp_path, SESSION_A).write_bytes(b"\xff\xfe not json")

    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_A)

    assert error.value.code == "SESSION_INVALID"


def test_a_session_whose_top_level_is_not_an_object_is_refused(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A)
    session_path(tmp_path, SESSION_A).write_text("[]", encoding="utf-8")

    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_A)

    assert error.value.code == "SESSION_INVALID"


def test_deleting_a_session_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SessionError) as error:
        delete_session(tmp_path, SESSION_A)

    assert error.value.code == "SESSION_NOT_FOUND"


def test_listing_skips_an_unreadable_session_without_failing(tmp_path: Path) -> None:
    create_session(tmp_path, session_id=SESSION_A, now="2026-09-15T00:00:00.000000Z")
    create_session(tmp_path, session_id=SESSION_B, now="2026-09-15T00:00:01.000000Z")
    session_path(tmp_path, SESSION_B).write_text("{}", encoding="utf-8")

    summaries = list_sessions(tmp_path)

    assert [item.session_id for item in summaries] == [SESSION_A]
    # Loading it by name still reports exactly why it could not be read.
    with pytest.raises(SessionError) as error:
        load_session(tmp_path, SESSION_B)
    assert error.value.code == "SESSION_INVALID"
