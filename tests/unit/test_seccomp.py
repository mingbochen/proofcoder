"""Offline tests for the socket filter that closes what Landlock leaves open (ADR-0011).

The filter's decisions are checked on every platform by evaluating the program it would
install. The tests that prove the gap is closed need a Linux kernel with Landlock and a
supported architecture, and are skipped elsewhere with the reason.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
from pathlib import Path

import pytest

from proofcoder.safety import landlock, seccomp
from proofcoder.safety.sandbox import SandboxSettings, SandboxState, decide_sandbox
from proofcoder.tools.command import create_run_command_tool

PROBE = landlock.probe()
needs_socket_filter = pytest.mark.skipif(
    PROBE.abi is None or not seccomp.available(),
    reason=f"needs Landlock and a supported architecture here: {PROBE.reason or 'no filter'}",
)
X86_64 = seccomp.ARCHITECTURES["x86_64"]
AARCH64 = seccomp.ARCHITECTURES["aarch64"]
AF_INET = 2


def _evaluate(program: list[seccomp.Instruction], data: bytes) -> int:
    """Run a classic BPF program over one ``seccomp_data`` buffer.

    Only the four instructions the filter uses are implemented; anything else fails the
    test, so the program cannot quietly grow an instruction this does not check.
    """

    accumulator = 0
    index = 0
    while True:
        code, jump_true, jump_false, k = program[index]
        if code == 0x20:  # load word at absolute offset
            accumulator = struct.unpack_from("<I", data, k)[0]
            index += 1
        elif code == 0x15:  # jump if equal
            index += 1 + (jump_true if accumulator == k else jump_false)
        elif code == 0x35:  # jump if greater or equal
            index += 1 + (jump_true if accumulator >= k else jump_false)
        elif code == 0x06:  # return
            return k
        else:
            raise AssertionError(f"unexpected BPF instruction {code:#x}")


def _data(architecture: seccomp.Architecture, nr: int, arg0: int = 0) -> bytes:
    # struct seccomp_data: int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6]
    return struct.pack("<iIQ6Q", nr, architecture.audit_arch, 0, arg0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("architecture", [X86_64, AARCH64])
def test_the_filter_refuses_unix_sockets_and_allows_the_rest(
    architecture: seccomp.Architecture,
) -> None:
    program = seccomp.build_program(architecture, deny_all_sockets=False)

    unix = _evaluate(program, _data(architecture, architecture.socket, seccomp.AF_UNIX))
    inet = _evaluate(program, _data(architecture, architecture.socket, AF_INET))
    other = _evaluate(program, _data(architecture, 0))

    assert unix == seccomp.RET_DENY
    assert inet == seccomp.RET_ALLOW
    assert other == seccomp.RET_ALLOW


@pytest.mark.parametrize("architecture", [X86_64, AARCH64])
def test_with_the_network_denied_every_socket_is_refused(
    architecture: seccomp.Architecture,
) -> None:
    program = seccomp.build_program(architecture, deny_all_sockets=True)

    for family in (seccomp.AF_UNIX, AF_INET, 10, 40):
        assert _evaluate(program, _data(architecture, architecture.socket, family)) == (
            seccomp.RET_DENY
        )
    assert _evaluate(program, _data(architecture, 0)) == seccomp.RET_ALLOW


def test_io_uring_x32_and_foreign_architectures_are_closed() -> None:
    program = seccomp.build_program(X86_64, deny_all_sockets=False)
    foreign = struct.pack("<iIQ6Q", 41, 0x40000003, 0, 1, 0, 0, 0, 0, 0)  # i386 entry

    assert _evaluate(program, _data(X86_64, X86_64.io_uring_setup)) == seccomp.RET_DENY
    assert _evaluate(program, _data(X86_64, 0x40000000 | 41, seccomp.AF_UNIX)) == (seccomp.RET_DENY)
    assert _evaluate(program, foreign) == seccomp.RET_KILL_PROCESS


def test_other_platforms_and_architectures_have_no_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert seccomp.current_architecture() is None
    assert not seccomp.available()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(seccomp.platform, "machine", lambda: "riscv64")
    assert seccomp.current_architecture() is None
    with pytest.raises(seccomp.SeccompError, match="riscv64"):
        seccomp.install(deny_all_sockets=True)


def test_a_filter_that_cannot_be_installed_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    import proofcoder.sandbox_exec as wrapper

    def refuse(**kwargs: object) -> None:
        raise seccomp.SeccompError("installing the socket filter failed (errno 22)")

    class _NoLimits:
        RLIMIT_CORE = RLIMIT_FSIZE = RLIMIT_CPU = RLIMIT_NOFILE = 0
        RLIM_INFINITY = -1

        def getrlimit(self, which: int) -> tuple[int, int]:
            return (-1, -1)

        def setrlimit(self, which: int, value: tuple[int, int]) -> None:
            pass

    monkeypatch.setattr(wrapper, "resource", _NoLimits())
    monkeypatch.setattr(wrapper, "restrict_self", lambda rules, **kwargs: None)
    monkeypatch.setattr(wrapper, "install_socket_filter", refuse)
    monkeypatch.setattr(os, "execv", lambda *args: pytest.fail("the command must not start"))
    config = {
        "version": 1,
        "abi": 7,
        "rules": [{"path": str(tmp_path), "access": "full", "optional": False}],
        "deny_tcp": True,
        "scope": True,
        "sockets": {"restrict": True, "deny_all": True},
        "rlimits": {"core": 0, "fsize": 1024, "nofile": 64, "cpu": 10},
    }

    code = wrapper.main([json.dumps(config), "--", str(tmp_path / "tool")])

    assert code == 125
    assert "socket filter failed" in capfd.readouterr().err


# ---------------------------------------------------------------- the gap is closed


def _environment(workspace: Path) -> dict[str, str]:
    return {
        "PATH": os.pathsep.join(
            dict.fromkeys([str(Path(sys.executable).resolve().parent), "/usr/bin", "/bin"])
        ),
        "TMPDIR": str(workspace),
    }


def _outcomes(workspace: Path, body: str, state: SandboxState) -> dict[str, str]:
    (workspace / "probe.py").write_text(
        "import asyncio, multiprocessing, os, socket, subprocess, sys\n"
        "def check(label, action):\n"
        "    try:\n"
        "        action()\n"
        "        print(label, 'allowed')\n"
        "    except OSError:\n"
        "        print(label, 'denied')\n" + body,
        encoding="utf-8",
    )
    tool = create_run_command_tool(workspace, environ=_environment(workspace), sandbox=state)
    result = tool.execute({"argv": ["python", "probe.py"], "cwd": ".", "timeout_seconds": 60})
    assert result.ok, result
    assert result.data is not None and result.data["sandboxed"] is True
    lines = str(result.data["stdout"]).splitlines()
    return dict(line.rsplit(" ", 1) for line in lines if line.endswith(("allowed", "denied")))


@needs_socket_filter
def test_a_service_socket_outside_the_workspace_is_out_of_reach(tmp_path: Path) -> None:
    """The measured L.4 gap: a connect by path to a listening socket outside."""

    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = outside / "service.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        outcomes = _outcomes(
            workspace,
            "def connect():\n"
            "    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            f"    client.connect({str(path)!r})\n"
            "check('unix_connect', connect)\n"
            "check('udp', lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)"
            ".sendto(b'x', ('127.0.0.1', 9)))\n"
            "check('socketpair', lambda: socket.socketpair())\n"
            "check('asyncio', lambda: asyncio.run(asyncio.sleep(0)))\n"
            "check('pipe', lambda: multiprocessing.Pipe())\n"
            "child = subprocess.run([sys.executable, '-c', 'import socket; "
            "socket.socket(socket.AF_UNIX)'], capture_output=True)\n"
            "print('child', 'denied' if child.returncode else 'allowed')\n",
            decide_sandbox(SandboxSettings()),
        )
    finally:
        server.close()

    assert outcomes == {
        "unix_connect": "denied",
        "udp": "denied",
        "socketpair": "allowed",
        "asyncio": "allowed",
        "pipe": "allowed",
        "child": "denied",
    }


@needs_socket_filter
def test_allowing_the_network_opens_tcp_and_never_unix_sockets(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        outcomes = _outcomes(
            tmp_path,
            f"check('tcp', lambda: socket.create_connection(('127.0.0.1', {port}), 5))\n"
            "check('unix', lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))\n",
            decide_sandbox(SandboxSettings(allow_network=True)),
        )
    finally:
        listener.close()

    assert outcomes == {"tcp": "allowed", "unix": "denied"}
