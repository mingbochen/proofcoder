"""A seccomp-bpf filter that stops an isolated command from opening socket channels.

Landlock governs files, not a ``connect`` to a socket file that already exists, and its
network rules cover TCP only. So a command confined by Landlock could still reach a
local service such as the Docker daemon or a D-Bus session bus by connecting to its
socket, and have that service act outside the sandbox for it. ADR-0011 closes the gap
one step earlier: the command may not create the socket at all.

seccomp can only see a system call's number and integer arguments, never the address a
``connect`` points at, so the rule is on ``socket()`` and its address family:

- ``socket(AF_UNIX, ...)`` is always refused. ``socketpair`` is a different call and is
  not touched, so asyncio and multiprocessing keep their unnamed pairs.
- With the network denied, ``socket()`` is refused for every family, which closes UDP
  and the rest along with TCP.
- ``io_uring_setup`` is refused, because io_uring can create a socket without calling
  ``socket()``.
- A call made through another architecture's entry point kills the process, since the
  call numbers above would not mean the same thing there.

Refusals return ``EACCES``: the command sees an ordinary permission error.
"""

from __future__ import annotations

import ctypes
import platform
import sys
from dataclasses import dataclass

_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06

RET_ALLOW = 0x7FFF0000
RET_ERRNO = 0x00050000
RET_KILL_PROCESS = 0x80000000
EACCES = 13
RET_DENY = RET_ERRNO | EACCES

_OFFSET_NR = 0
_OFFSET_ARCH = 4
# args[0] starts after nr (4), arch (4) and instruction_pointer (8). Both supported
# architectures are little-endian, so the low 32 bits of the first argument are here.
_OFFSET_ARG0_LOW = 16
_X32_SYSCALL_BIT = 0x40000000
AF_UNIX = 1

_PR_GET_SECCOMP = 21
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


@dataclass(frozen=True, slots=True)
class Architecture:
    """The numbers a filter needs for one architecture."""

    audit_arch: int
    socket: int
    io_uring_setup: int


ARCHITECTURES = {
    "x86_64": Architecture(audit_arch=0xC000003E, socket=41, io_uring_setup=425),
    "amd64": Architecture(audit_arch=0xC000003E, socket=41, io_uring_setup=425),
    "aarch64": Architecture(audit_arch=0xC00000B7, socket=198, io_uring_setup=425),
    "arm64": Architecture(audit_arch=0xC00000B7, socket=198, io_uring_setup=425),
}

Instruction = tuple[int, int, int, int]


class SeccompError(Exception):
    """Raised when the filter cannot be installed exactly as built."""


def current_architecture() -> Architecture | None:
    """Return this machine's filter numbers, or None where the filter is not offered."""

    if not sys.platform.startswith("linux"):
        return None
    return ARCHITECTURES.get(platform.machine().casefold())


def available() -> bool:
    """Whether this host can have the socket filter at all.

    Asks the kernel rather than assuming: ``PR_GET_SECCOMP`` fails on a kernel built
    without seccomp, and reading the mode restricts nothing. Without this, a state could
    promise a filter that every command then fails to install.
    """

    if current_architecture() is None:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return False
    libc.prctl.restype = ctypes.c_int
    mode = libc.prctl(
        ctypes.c_int(_PR_GET_SECCOMP),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    )
    return int(mode) >= 0


def build_program(architecture: Architecture, *, deny_all_sockets: bool) -> list[Instruction]:
    """Build the filter as ``(code, jump_true, jump_false, k)`` instructions.

    Pure, so its decisions can be checked on any platform by evaluating it.
    """

    program: list[Instruction] = [
        (_BPF_LD_W_ABS, 0, 0, _OFFSET_ARCH),
        (_BPF_JEQ_K, 1, 0, architecture.audit_arch),
        (_BPF_RET_K, 0, 0, RET_KILL_PROCESS),
        (_BPF_LD_W_ABS, 0, 0, _OFFSET_NR),
        (_BPF_JGE_K, 0, 1, _X32_SYSCALL_BIT),
        (_BPF_RET_K, 0, 0, RET_DENY),
        (_BPF_JEQ_K, 0, 1, architecture.io_uring_setup),
        (_BPF_RET_K, 0, 0, RET_DENY),
        (_BPF_JEQ_K, 1, 0, architecture.socket),
        (_BPF_RET_K, 0, 0, RET_ALLOW),
    ]
    if deny_all_sockets:
        program.append((_BPF_RET_K, 0, 0, RET_DENY))
        return program
    program.extend(
        [
            (_BPF_LD_W_ABS, 0, 0, _OFFSET_ARG0_LOW),
            (_BPF_JEQ_K, 0, 1, AF_UNIX),
            (_BPF_RET_K, 0, 0, RET_DENY),
            (_BPF_RET_K, 0, 0, RET_ALLOW),
        ]
    )
    return program


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter)))


def install(*, deny_all_sockets: bool) -> None:
    """Install the filter on the calling process, irreversibly.

    The caller must already have set ``no_new_privs``; the kernel refuses an
    unprivileged filter otherwise, and that refusal is raised like any other.
    """

    architecture = current_architecture()
    if architecture is None:
        raise SeccompError(f"no socket filter for architecture {platform.machine()}")
    program = build_program(architecture, deny_all_sockets=deny_all_sockets)
    instructions = (_SockFilter * len(program))(*(_SockFilter(*item) for item in program))
    fprog = _SockFprog(len(program), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(
        ctypes.c_int(_PR_SET_SECCOMP),
        ctypes.c_ulong(_SECCOMP_MODE_FILTER),
        ctypes.byref(fprog),
        ctypes.c_ulong(0),
        ctypes.c_ulong(0),
    ):
        error = ctypes.get_errno()
        raise SeccompError(f"installing the socket filter failed (errno {error})")
