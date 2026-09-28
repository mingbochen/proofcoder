"""Offline tests for operating-system command isolation.

The decision and planning tests run everywhere. The tests that prove isolation holds
need a Linux kernel that offers Landlock, and are skipped with the probe's own reason
where it does not: a skip there is the documented platform limit, not a pass.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import proofcoder.tools.command as command_module
from proofcoder.agent import AgentLoop
from proofcoder.llm.scripted import ScriptedClient
from proofcoder.protocol import CompletionStatus, FunctionCall, ModelResponse, ToolCall
from proofcoder.safety import landlock
from proofcoder.safety.landlock import AccessClass, LandlockProbe
from proofcoder.safety.sandbox import (
    SETUP_FAILED_EXIT_CODE,
    SETUP_FAILED_MARKER,
    TEMPORARY_ROOT,
    WRAPPER_MODULE,
    SandboxMode,
    SandboxSettings,
    SandboxState,
    SandboxStatus,
    decide_sandbox,
    install_root,
    is_setup_failure,
    sandbox_payload,
    wrapped_argv,
)
from proofcoder.tools.command import create_run_command_tool
from proofcoder.tools.edit import create_create_file_tool
from proofcoder.tools.finish import create_finish_task_tool
from proofcoder.tools.registry import ToolRegistry

SENSITIVE_SENTINEL = "never-readable-from-inside-the-sandbox"
PROBE = landlock.probe()
needs_landlock = pytest.mark.skipif(
    PROBE.abi is None, reason=f"Landlock is not available here: {PROBE.reason}"
)


def _probe(abi: int | None, reason: str | None = None) -> object:
    return lambda: LandlockProbe(abi=abi, reason=reason)


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(
            dict.fromkeys([str(Path(sys.executable).resolve().parent), "/usr/bin", "/bin"])
        ),
        "TMPDIR": str(workspace),
    }


def _run(
    workspace: Path, argv: list[str], state: SandboxState | None, timeout: int = 60
) -> dict[str, object]:
    tool = create_run_command_tool(workspace, environ=_environment(workspace), sandbox=state)
    result = tool.execute({"argv": argv, "cwd": ".", "timeout_seconds": timeout})
    payload: dict[str, object] = dict(result.data or {})
    payload["ok"] = result.ok
    payload["error_code"] = None if result.error is None else result.error.code
    return payload


def _enforced(**settings: object) -> SandboxState:
    return decide_sandbox(SandboxSettings(**settings))  # type: ignore[arg-type]


# ---------------------------------------------------------------- deciding


def test_off_never_probes() -> None:
    def forbidden() -> LandlockProbe:
        raise AssertionError("off must not probe")

    state = decide_sandbox(SandboxSettings(mode=SandboxMode.OFF), probe=forbidden)

    assert state.status is SandboxStatus.OFF
    assert not state.isolates
    assert not state.satisfies_required


def test_a_failed_probe_is_unavailable_on_linux_and_unsupported_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    linux = decide_sandbox(SandboxSettings(), probe=_probe(None, "disabled at boot"))
    monkeypatch.setattr(sys, "platform", "win32")
    windows = decide_sandbox(SandboxSettings(), probe=_probe(None, "not Linux"))

    assert linux.status is SandboxStatus.UNAVAILABLE
    assert linux.reason == "disabled at boot"
    assert windows.status is SandboxStatus.UNSUPPORTED
    assert not linux.isolates and not windows.isolates


def test_an_old_kernel_is_partial_unless_the_network_was_allowed() -> None:
    partial = decide_sandbox(SandboxSettings(), probe=_probe(3))
    allowed = decide_sandbox(SandboxSettings(allow_network=True), probe=_probe(3))

    assert partial.status is SandboxStatus.PARTIAL
    assert partial.isolates and not partial.satisfies_required
    assert partial.filesystem and not partial.tcp_restricted
    assert "TCP" in (partial.reason or "")
    assert allowed.status is SandboxStatus.ENFORCED
    assert allowed.satisfies_required


def test_a_current_kernel_restricts_tcp_and_scopes() -> None:
    state = decide_sandbox(SandboxSettings(), probe=_probe(7))

    assert state.status is SandboxStatus.ENFORCED
    assert state.tcp_restricted and state.scoped
    assert decide_sandbox(SandboxSettings(), probe=_probe(5)).scoped is False


def test_the_payload_counts_extra_paths_and_never_lists_them() -> None:
    settings = SandboxSettings(extra_read_paths=(Path("/opt/private-toolchain"),))
    payload = sandbox_payload(decide_sandbox(settings, probe=_probe(7)))

    assert payload["extra_read_paths"] == 1
    assert "private-toolchain" not in json.dumps(payload)
    assert payload["status"] == "enforced"


# ---------------------------------------------------------------- planning


def test_install_root_takes_the_prefix_above_bin() -> None:
    assert install_root(Path("/home/u/.venv/bin/python")) == Path("/home/u/.venv")
    assert install_root(Path("/opt/tool/node")) == Path("/opt/tool")


def test_wrapped_argv_starts_isolated_and_ends_with_the_command(tmp_path: Path) -> None:
    state = decide_sandbox(SandboxSettings(), probe=_probe(7))
    argv = wrapped_argv(
        ("/usr/bin/git", "status"),
        workspace=tmp_path,
        temporary=tmp_path / "t",
        state=state,
        timeout_seconds=30,
    )

    # -P and -E: nothing from the working directory or PYTHON* variables is imported
    # while the wrapper is still unrestricted.
    assert argv[:5] == [sys.executable, "-P", "-E", "-m", WRAPPER_MODULE]
    assert argv[6:] == ["--", "/usr/bin/git", "status"]
    config = json.loads(argv[5])
    rules = {rule["path"]: rule for rule in config["rules"]}
    assert rules[str(tmp_path)] == {"path": str(tmp_path), "access": "full", "optional": False}
    assert rules[str(tmp_path / "t")]["optional"] is False
    assert rules["/usr"]["access"] == AccessClass.READ_EXECUTE.value
    assert rules["/dev/null"]["access"] == AccessClass.READ_WRITE_FILE.value
    assert config["rlimits"]["cpu"] == 35
    assert config["deny_tcp"] is True and config["scope"] is True


def test_wrapping_needs_a_kernel_abi(tmp_path: Path) -> None:
    state = decide_sandbox(SandboxSettings(mode=SandboxMode.OFF))

    with pytest.raises(ValueError):
        wrapped_argv(("x",), workspace=tmp_path, temporary=tmp_path, state=state, timeout_seconds=1)


def test_setup_failure_needs_both_the_code_and_the_marker() -> None:
    assert is_setup_failure(SETUP_FAILED_EXIT_CODE, f"{SETUP_FAILED_MARKER} no rules\n")
    assert not is_setup_failure(SETUP_FAILED_EXIT_CODE, "a command's own exit code 125\n")
    assert not is_setup_failure(1, f"{SETUP_FAILED_MARKER} no rules\n")


def test_access_bits_follow_the_abi() -> None:
    assert landlock.handled_fs_access(1) == (1 << 13) - 1
    assert landlock.handled_fs_access(3) & landlock.ACCESS_FS_TRUNCATE
    assert not landlock.handled_fs_access(2) & landlock.ACCESS_FS_TRUNCATE
    read = landlock.access_bits(AccessClass.READ_EXECUTE, 7)
    assert read & landlock.ACCESS_FS_EXECUTE and not read & landlock.ACCESS_FS_WRITE_FILE


# ---------------------------------------------------------------- run_command


def test_without_a_state_the_result_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")

    result = _run(tmp_path, ["python", "ok.py"], None)

    assert result["ok"] is True
    assert "sandboxed" not in result


def test_a_state_that_does_not_isolate_runs_directly_and_says_so(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    state = decide_sandbox(SandboxSettings(mode=SandboxMode.OFF))

    result = _run(tmp_path, ["python", "ok.py"], state)

    assert result["ok"] is True
    assert result["sandboxed"] is False


def test_a_wrapper_that_cannot_isolate_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: the command's own first act would leave a marker, and none appears."""

    (tmp_path / "act.py").write_text(
        "open('ran.txt', 'w').write('the command ran')\n", encoding="utf-8"
    )
    real = command_module.wrapped_argv

    def broken(*args: object, **kwargs: object) -> list[str]:
        argv = real(*args, **kwargs)  # type: ignore[arg-type]
        config = json.loads(argv[5])
        config["version"] = 0
        argv[5] = json.dumps(config)
        return argv

    monkeypatch.setattr(command_module, "wrapped_argv", broken)
    state = decide_sandbox(SandboxSettings(), probe=_probe(7))

    result = _run(tmp_path, ["python", "act.py"], state)

    assert result["ok"] is False
    assert result["error_code"] == "SANDBOX_SETUP_FAILED"
    assert result["sandboxed"] is True
    assert not (tmp_path / "ran.txt").exists()
    assert list((tmp_path / TEMPORARY_ROOT).iterdir()) == []


@pytest.mark.parametrize(
    "config",
    ["not json", json.dumps({"version": 1}), json.dumps({"version": 1, "rlimits": {}})],
)
def test_the_wrapper_rejects_malformed_configuration(tmp_path: Path, config: str) -> None:
    marker = tmp_path / "ran.txt"

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-E",
            "-m",
            WRAPPER_MODULE,
            config,
            "--",
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').write('x')",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == SETUP_FAILED_EXIT_CODE
    assert completed.stderr.startswith(SETUP_FAILED_MARKER)
    assert not marker.exists()


# ---------------------------------------------------------------- isolation holds


def _probe_script(workspace: Path, body: str) -> None:
    (workspace / "probe.py").write_text(
        "import os, sys\n"
        "def check(label, action):\n"
        "    try:\n"
        "        action()\n"
        "        print(label, 'allowed')\n"
        "    except OSError:\n"
        "        print(label, 'denied')\n" + body,
        encoding="utf-8",
    )


def _outcomes(result: dict[str, object]) -> dict[str, str]:
    assert result["ok"] is True, result
    lines = str(result["stdout"]).splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if line.endswith(("allowed", "denied")))


@needs_landlock
def test_a_workspace_script_cannot_reach_host_files(tmp_path: Path) -> None:
    """Stage L's exit criterion: nothing outside the workspace is readable or writable."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text(SENSITIVE_SENTINEL, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inside.txt").write_text("inside", encoding="utf-8")
    _probe_script(
        workspace,
        f"check('read_inside', lambda: open('inside.txt').read())\n"
        f"check('write_inside', lambda: open('new.txt', 'w').write('x'))\n"
        f"check('read_outside', lambda: print(open({str(outside / 'secret.txt')!r}).read()))\n"
        f"check('write_outside', lambda: open({str(outside / 'planted.txt')!r}, 'w'))\n"
        f"check('list_outside', lambda: os.listdir({str(outside)!r}))\n"
        "check('parent_environ', lambda: open(f'/proc/{os.getppid()}/environ', 'rb').read())\n"
        "check('own_status', lambda: open('/proc/self/status').read())\n",
    )

    result = _run(workspace, ["python", "probe.py"], _enforced())
    outcomes = _outcomes(result)

    assert result["sandboxed"] is True
    assert outcomes == {
        "read_inside": "allowed",
        "write_inside": "allowed",
        "read_outside": "denied",
        "write_outside": "denied",
        "list_outside": "denied",
        "parent_environ": "denied",
        "own_status": "allowed",
    }
    assert SENSITIVE_SENTINEL not in str(result["stdout"])
    assert not (outside / "planted.txt").exists()


@needs_landlock
def test_temporary_files_go_to_the_private_directory_and_are_removed(tmp_path: Path) -> None:
    (tmp_path / "tmp.py").write_text(
        "import tempfile\nfd, name = tempfile.mkstemp()\nprint(name)\n", encoding="utf-8"
    )

    result = _run(tmp_path, ["python", "tmp.py"], _enforced())

    created = Path(str(result["stdout"]).strip())
    assert created.parent.parent == tmp_path / TEMPORARY_ROOT
    assert not created.parent.exists()


@needs_landlock
def test_a_path_the_user_adds_is_readable_and_not_writable(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "data.txt").write_text("shared", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _probe_script(
        workspace,
        f"check('read_added', lambda: open({str(shared / 'data.txt')!r}).read())\n"
        f"check('write_added', lambda: open({str(shared / 'new.txt')!r}, 'w'))\n",
    )

    outcomes = _outcomes(
        _run(workspace, ["python", "probe.py"], _enforced(extra_read_paths=(shared,)))
    )

    assert outcomes == {"read_added": "allowed", "write_added": "denied"}


@needs_landlock
@pytest.mark.skipif(
    PROBE.abi is None or PROBE.abi < landlock.TCP_ABI, reason="TCP needs Landlock ABI 4"
)
@pytest.mark.parametrize("allow_network", [False, True])
def test_tcp_is_denied_unless_the_user_allows_it(tmp_path: Path, allow_network: bool) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        _probe_script(
            tmp_path,
            "import socket\n"
            f"check('connect', lambda: socket.create_connection(('127.0.0.1', {port}), 5))\n",
        )
        outcomes = _outcomes(
            _run(tmp_path, ["python", "probe.py"], _enforced(allow_network=allow_network))
        )
    finally:
        listener.close()

    assert outcomes == {"connect": "allowed" if allow_network else "denied"}


@needs_landlock
def test_resource_limits_are_in_force(tmp_path: Path) -> None:
    (tmp_path / "limits.py").write_text(
        "import resource\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE)[0])\n"
        "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])\n"
        "print(resource.getrlimit(resource.RLIMIT_CPU)[0])\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, ["python", "limits.py"], _enforced(), timeout=20)
    core, nofile, cpu = (int(line) for line in str(result["stdout"]).split())

    assert core == 0
    assert nofile <= 4096
    assert cpu == 25


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        content=None,
        reasoning_content=None,
        finish_reason="tool_calls",
        usage=None,
        tool_calls=(
            ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments))),
        ),
    )


@needs_landlock
def test_a_test_run_inside_isolation_still_verifies_the_run(tmp_path: Path) -> None:
    """Isolation changes where a command may reach, never what its success means."""

    (tmp_path / "tests").mkdir()
    registry = ToolRegistry()
    registry.register(create_create_file_tool(tmp_path, checkpoint_available=True))
    registry.register(
        create_run_command_tool(tmp_path, environ=_environment(tmp_path), sandbox=_enforced())
    )
    registry.register(create_finish_task_tool(tmp_path))
    test_source = (
        "import unittest\n\nclass T(unittest.TestCase):\n    def test(self):\n        pass\n"
    )
    client = ScriptedClient(
        [
            _call("c1", "create_file", {"path": "tests/test_it.py", "content": test_source}),
            _call(
                "c2",
                "run_command",
                {"argv": ["python", "-m", "unittest", "discover", "-s", "tests"]},
            ),
            _call("c3", "finish_task", {"summary": "added a test"}),
        ]
    )

    result = AgentLoop(
        client=client,
        registry=registry,
        workspace=tmp_path,
        system_prompt="test system",
        max_steps=4,
    ).run("add a test")

    assert result.completion_status is CompletionStatus.COMPLETED_VERIFIED
    # The evidence names the command the model asked for, not the wrapper that ran it.
    assert result.verification_command == ("python", "-m", "unittest", "discover", "-s", "tests")


# ---------------------------------------------------------------- the wrapper, in process
#
# These call the wrapper's own functions with the final, irreversible steps replaced -
# resource limits, the restriction and the exec - so the test process is never
# restricted and they run on every platform.


class _FakeResource:
    RLIMIT_CORE = 4
    RLIMIT_FSIZE = 1
    RLIMIT_CPU = 0
    RLIMIT_NOFILE = 7
    RLIM_INFINITY = -1

    def __init__(self, current: tuple[int, int] = (-1, -1)) -> None:
        self.current = current
        self.applied: dict[int, tuple[int, int]] = {}

    def getrlimit(self, which: int) -> tuple[int, int]:
        return self.current

    def setrlimit(self, which: int, value: tuple[int, int]) -> None:
        self.applied[which] = value


def _wrapper_config(tmp_path: Path, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "version": 1,
        "abi": 7,
        "rules": [
            {"path": str(tmp_path), "access": "full", "optional": False},
            {"path": str(tmp_path / "absent"), "access": "read_execute", "optional": True},
        ],
        "deny_tcp": True,
        "scope": True,
        "rlimits": {"core": 0, "fsize": 1024, "nofile": 64, "cpu": 10},
    }
    config.update(overrides)
    return config


def _stub_wrapper(monkeypatch: pytest.MonkeyPatch, resource: _FakeResource) -> dict[str, object]:
    import proofcoder.sandbox_exec as wrapper

    restricted: dict[str, object] = {}
    monkeypatch.setattr(wrapper, "resource", resource)
    monkeypatch.setattr(
        wrapper, "restrict_self", lambda rules, **kwargs: restricted.update(rules=rules, **kwargs)
    )
    return restricted


def test_the_wrapper_applies_limits_and_rules_then_execs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import proofcoder.sandbox_exec as wrapper

    resource = _FakeResource()
    restricted = _stub_wrapper(monkeypatch, resource)
    executed: list[list[str]] = []

    def fake_exec(path: str, argv: list[str]) -> None:
        executed.append(argv)
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(os, "execv", fake_exec)
    tool = str(tmp_path / "tool")

    code = wrapper.main([json.dumps(_wrapper_config(tmp_path)), "--", tool, "x"])

    assert code == SETUP_FAILED_EXIT_CODE  # only because the fake exec failed
    assert executed == [[tool, "x"]]
    assert resource.applied == {
        resource.RLIMIT_CORE: (0, 0),
        resource.RLIMIT_FSIZE: (1024, 1024),
        resource.RLIMIT_CPU: (10, 10),
        resource.RLIMIT_NOFILE: (64, -1),
    }
    paths = [path for path, _ in restricted["rules"]]  # type: ignore[attr-defined]
    # The optional rule that does not exist is dropped; the process's own /proc entry
    # is added last and no other process's entry is.
    assert paths == [str(tmp_path), f"/proc/{os.getpid()}"]
    assert restricted["deny_tcp"] is True and restricted["scope"] is True


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"version": 2}, "version"),
        ({"rules": []}, "rules are missing"),
        ({"rules": ["x"]}, "not an object"),
        ({"rules": [{"path": "relative", "access": "full", "optional": False}]}, "malformed"),
        ({"rules": [{"path": "ABSENT", "access": "full", "optional": False}]}, "does not exist"),
        ({"rules": [{"path": "HERE", "access": "everything", "optional": False}]}, "everything"),
        ({"deny_tcp": "yes"}, "network settings"),
        ({"abi": -1}, "abi"),
        ({"rlimits": None}, "rlimits are missing"),
        ({"rlimits": {"core": 0}}, "fsize"),
    ],
)
def test_every_malformed_wrapper_input_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    override: dict[str, object],
    fragment: str,
) -> None:
    import proofcoder.sandbox_exec as wrapper

    _stub_wrapper(monkeypatch, _FakeResource())
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("the command must not start"))
    rules = override.get("rules")
    if isinstance(rules, list) and rules and isinstance(rules[0], dict):
        rule = dict(rules[0])
        rule["path"] = {"ABSENT": str(tmp_path / "absent"), "HERE": str(tmp_path)}.get(
            str(rule["path"]), rule["path"]
        )
        override = {**override, "rules": [rule]}

    config = json.dumps(_wrapper_config(tmp_path, **override))
    code = wrapper.main([config, "--", str(tmp_path / "tool")])

    assert code == SETUP_FAILED_EXIT_CODE
    error = capfd.readouterr().err
    assert error.startswith(SETUP_FAILED_MARKER)
    assert fragment in error


def test_the_wrapper_needs_json_an_absolute_executable_and_a_separator(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    import proofcoder.sandbox_exec as wrapper

    config = json.dumps(_wrapper_config(tmp_path))

    assert wrapper.main(["{", "--", str(tmp_path / "tool")]) == SETUP_FAILED_EXIT_CODE
    assert wrapper.main([config, "--", "tool"]) == SETUP_FAILED_EXIT_CODE
    assert wrapper.main([config, "tool"]) == SETUP_FAILED_EXIT_CODE
    assert capfd.readouterr().err.count(SETUP_FAILED_MARKER) == 3


def test_without_resource_limits_the_wrapper_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import proofcoder.sandbox_exec as wrapper

    monkeypatch.setattr(wrapper, "resource", None)

    code = wrapper.main([json.dumps(_wrapper_config(tmp_path)), "--", str(tmp_path / "tool")])

    assert code == SETUP_FAILED_EXIT_CODE
    assert "not available" in capfd.readouterr().err


def test_limits_are_only_ever_lowered(monkeypatch: pytest.MonkeyPatch) -> None:
    import proofcoder.sandbox_exec as wrapper

    resource = _FakeResource(current=(5, 8))
    monkeypatch.setattr(wrapper, "resource", resource)

    wrapper._set_limit(resource.RLIMIT_CPU, 100)

    assert resource.applied == {resource.RLIMIT_CPU: (5, 8)}


# ---------------------------------------------------------------- the bindings


@needs_landlock
def test_a_bad_rule_fails_before_the_process_is_restricted(tmp_path: Path) -> None:
    """Creating a ruleset restricts nothing, so this can run in the test process."""

    with pytest.raises(landlock.LandlockError, match="opening"):
        landlock.restrict_self(
            [(str(tmp_path), AccessClass.FULL), (str(tmp_path / "absent"), AccessClass.FULL)],
            abi=PROBE.abi or 1,
            deny_tcp=False,
            scope=False,
        )
    # Still unrestricted: a file outside tmp_path is readable.
    assert Path(__file__).read_text(encoding="utf-8")


def test_capabilities_the_abi_lacks_are_refused() -> None:
    with pytest.raises(landlock.LandlockError, match="TCP"):
        landlock.restrict_self([], abi=3, deny_tcp=True, scope=False)
    with pytest.raises(landlock.LandlockError, match="scope"):
        landlock.restrict_self([], abi=5, deny_tcp=False, scope=True)


def test_probe_failures_are_explained() -> None:
    assert "without Landlock" in landlock._probe_failure(38)
    assert "disabled at boot" in landlock._probe_failure(95)
    assert "seccomp" in landlock._probe_failure(1)
    assert "errno 22" in landlock._probe_failure(22)


def test_the_probe_reports_other_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert landlock.probe() == LandlockProbe(abi=None, reason="not Linux")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(landlock.platform, "machine", lambda: "mips")
    assert landlock.probe().reason == "unsupported architecture mips"
