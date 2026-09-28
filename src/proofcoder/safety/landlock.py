"""Linux Landlock through ``ctypes``: probe the kernel, and restrict the calling process.

Landlock lets an unprivileged process give up access it has, and every process it later
starts inherits the restriction; nothing can take it back. Three system calls and three
structures are all it takes, so this module calls them directly rather than adding a
dependency. It is imported on every platform and does nothing outside Linux.

Only the execution wrapper ever calls :func:`restrict_self`. Calling it in the
ProofCoder process would restrict that process for good.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from dataclasses import dataclass
from enum import StrEnum

# The three calls were added after the syscall tables were unified, so they share these
# numbers on every architecture that uses the generic table.
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_GENERIC_TABLE_MACHINES = frozenset(
    {"x86_64", "amd64", "aarch64", "arm64", "armv7l", "armv8l", "riscv64", "ppc64le", "s390x"}
)

_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

ACCESS_FS_EXECUTE = 1 << 0
ACCESS_FS_WRITE_FILE = 1 << 1
ACCESS_FS_READ_FILE = 1 << 2
ACCESS_FS_READ_DIR = 1 << 3
ACCESS_FS_REMOVE_DIR = 1 << 4
ACCESS_FS_REMOVE_FILE = 1 << 5
ACCESS_FS_MAKE_CHAR = 1 << 6
ACCESS_FS_MAKE_DIR = 1 << 7
ACCESS_FS_MAKE_REG = 1 << 8
ACCESS_FS_MAKE_SOCK = 1 << 9
ACCESS_FS_MAKE_FIFO = 1 << 10
ACCESS_FS_MAKE_BLOCK = 1 << 11
ACCESS_FS_MAKE_SYM = 1 << 12
ACCESS_FS_REFER = 1 << 13
ACCESS_FS_TRUNCATE = 1 << 14
ACCESS_FS_IOCTL_DEV = 1 << 15

ACCESS_NET_BIND_TCP = 1 << 0
ACCESS_NET_CONNECT_TCP = 1 << 1

SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1

# The first ABI that can restrict each capability.
TCP_ABI = 4
SCOPE_ABI = 6

# Rights that make sense on a regular file or device. Directory rights on a file rule
# are rejected by the kernel, so a rule on a file is masked down to these.
_FILE_ACCESS = (
    ACCESS_FS_EXECUTE
    | ACCESS_FS_WRITE_FILE
    | ACCESS_FS_READ_FILE
    | ACCESS_FS_TRUNCATE
    | ACCESS_FS_IOCTL_DEV
)


class AccessClass(StrEnum):
    """What one rule allows, named rather than spelled as bits in the wrapper's input."""

    FULL = "full"
    READ_EXECUTE = "read_execute"
    READ_WRITE_FILE = "read_write_file"
    READ_FILE = "read_file"


class LandlockError(Exception):
    """Raised when a restriction cannot be applied exactly as asked."""


@dataclass(frozen=True, slots=True)
class LandlockProbe:
    """Whether this kernel offers Landlock, and at which ABI version."""

    abi: int | None
    reason: str | None


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    )


def handled_fs_access(abi: int) -> int:
    """Return every filesystem right the given ABI can restrict.

    Handling every right the kernel knows is what makes the ruleset deny by default: a
    right that is not handled is not restricted at all.
    """

    access = (1 << 13) - 1
    if abi >= 2:
        access |= ACCESS_FS_REFER
    if abi >= 3:
        access |= ACCESS_FS_TRUNCATE
    if abi >= 5:
        access |= ACCESS_FS_IOCTL_DEV
    return access


def access_bits(access: AccessClass, abi: int) -> int:
    """Translate a named access class into the bits this ABI understands."""

    handled = handled_fs_access(abi)
    if access is AccessClass.FULL:
        return handled
    if access is AccessClass.READ_EXECUTE:
        return (ACCESS_FS_EXECUTE | ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR) & handled
    if access is AccessClass.READ_WRITE_FILE:
        return (
            ACCESS_FS_READ_FILE | ACCESS_FS_WRITE_FILE | ACCESS_FS_TRUNCATE | ACCESS_FS_READ_DIR
        ) & handled
    return (ACCESS_FS_READ_FILE | ACCESS_FS_READ_DIR) & handled


def probe() -> LandlockProbe:
    """Ask the kernel for its Landlock ABI version without restricting anything."""

    if not sys.platform.startswith("linux"):
        return LandlockProbe(abi=None, reason="not Linux")
    machine = platform.machine().casefold()
    if machine not in _GENERIC_TABLE_MACHINES:
        return LandlockProbe(abi=None, reason=f"unsupported architecture {machine}")
    try:
        libc = _libc()
    except OSError:
        return LandlockProbe(abi=None, reason="the C library could not be loaded")
    result = libc.syscall(
        ctypes.c_long(_SYS_CREATE_RULESET),
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(_CREATE_RULESET_VERSION),
    )
    if result < 0:
        error = ctypes.get_errno()
        return LandlockProbe(abi=None, reason=_probe_failure(error))
    return LandlockProbe(abi=int(result), reason=None)


def restrict_self(
    rules: list[tuple[str, AccessClass]],
    *,
    abi: int,
    deny_tcp: bool,
    scope: bool,
) -> None:
    """Restrict the calling process to ``rules``, irreversibly.

    Every rule path must exist; the caller decides beforehand which optional paths to
    drop. Any failure raises, and a caller that is about to run untrusted code must not
    carry on after one.
    """

    if deny_tcp and abi < TCP_ABI:
        raise LandlockError(f"TCP restriction needs Landlock ABI {TCP_ABI}, kernel has {abi}")
    if scope and abi < SCOPE_ABI:
        raise LandlockError(f"scope restriction needs Landlock ABI {SCOPE_ABI}, kernel has {abi}")
    libc = _libc()

    handled_fs = handled_fs_access(abi)
    attribute = _RulesetAttr(
        handled_access_fs=handled_fs,
        handled_access_net=(ACCESS_NET_BIND_TCP | ACCESS_NET_CONNECT_TCP) if deny_tcp else 0,
        scoped=(SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL) if scope else 0,
    )
    # Pass only as much of the structure as the requested fields need. Older kernels
    # accept a longer structure only when its tail is zero, and a shorter one always.
    size = 24 if scope else 16 if deny_tcp else 8
    ruleset_fd = libc.syscall(
        ctypes.c_long(_SYS_CREATE_RULESET),
        ctypes.byref(attribute),
        ctypes.c_size_t(size),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        raise LandlockError(_errno_message("creating the ruleset"))
    try:
        for path, access in rules:
            _add_path_rule(libc, int(ruleset_fd), path, access_bits(access, abi))
        if libc.prctl(
            ctypes.c_int(_PR_SET_NO_NEW_PRIVS),
            ctypes.c_ulong(1),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        ):
            raise LandlockError(_errno_message("setting no_new_privs"))
        if libc.syscall(
            ctypes.c_long(_SYS_RESTRICT_SELF), ctypes.c_int(int(ruleset_fd)), ctypes.c_uint32(0)
        ):
            raise LandlockError(_errno_message("restricting the process"))
    finally:
        os.close(int(ruleset_fd))


def _add_path_rule(libc: ctypes.CDLL, ruleset_fd: int, path: str, allowed: int) -> None:
    try:
        parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as error:
        raise LandlockError(f"opening {path!r} for a rule failed: {error.strerror}") from error
    try:
        if not os.path.isdir(path):
            allowed &= _FILE_ACCESS
        rule = _PathBeneathAttr(allowed_access=allowed, parent_fd=parent_fd)
        if libc.syscall(
            ctypes.c_long(_SYS_ADD_RULE),
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_RULE_PATH_BENEATH),
            ctypes.byref(rule),
            ctypes.c_uint32(0),
        ):
            raise LandlockError(_errno_message(f"adding a rule for {path!r}"))
    finally:
        os.close(parent_fd)


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _probe_failure(error: int) -> str:
    if error == 38:  # ENOSYS
        return "the kernel was built without Landlock, or a seccomp filter blocks it"
    if error == 95:  # EOPNOTSUPP
        return "Landlock is built in but disabled at boot"
    if error == 1:  # EPERM
        return "a seccomp filter or security policy refuses the Landlock system calls"
    return f"the Landlock probe failed with errno {error}"


def _errno_message(action: str) -> str:
    error = ctypes.get_errno()
    return f"{action} failed: {os.strerror(error)} (errno {error})"
