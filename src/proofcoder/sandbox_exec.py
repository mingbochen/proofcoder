"""The execution wrapper: restrict this process, then become the command.

``run_command`` starts this module instead of the command itself when isolation is in
force. It sets resource limits, applies the Landlock ruleset, and replaces itself with
the command through ``execve``, so the command keeps this process's identifier, output
pipes, and process group.

Everything before ``execve`` fails closed. A malformed configuration, a rule that cannot
be added, or a kernel that refuses the restriction ends this process with a fixed exit
code and a fixed first line on standard error, and the command never starts. There is
no path on which the command runs without the restriction it was promised.

The interpreter is started with ``-P -E`` so that nothing in the working directory or
the environment can be imported ahead of this module while it is still as privileged
as ProofCoder.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from types import ModuleType

from proofcoder.safety.landlock import AccessClass, LandlockError, restrict_self
from proofcoder.safety.sandbox import (
    SETUP_FAILED_EXIT_CODE,
    SETUP_FAILED_MARKER,
    WRAPPER_CONFIG_VERSION,
)
from proofcoder.safety.seccomp import SeccompError
from proofcoder.safety.seccomp import install as install_socket_filter

resource: ModuleType | None
try:
    import resource
except ImportError:  # pragma: no cover - only Windows lacks it, and Windows never wraps
    resource = None


class _SetupError(Exception):
    """Any reason the command must not start."""


def main(argv: Sequence[str]) -> int:
    """Restrict this process and exec the command; return only on failure."""

    try:
        config, command = _parse(argv)
        _apply_rlimits(config.get("rlimits"))
        _apply_landlock(config)
        _apply_socket_filter(config.get("sockets"))
    except (_SetupError, LandlockError, SeccompError, OSError, ValueError) as error:
        return _fail(str(error))
    try:
        os.execv(command[0], command)
    except OSError as error:
        return _fail(f"exec failed: {error.strerror}")
    return _fail("exec returned")  # pragma: no cover - execv never returns on success


def _parse(argv: Sequence[str]) -> tuple[dict[str, object], list[str]]:
    if len(argv) < 3 or argv[1] != "--":
        raise _SetupError("usage: sandbox_exec <config> -- <command...>")
    try:
        config = json.loads(argv[0])
    except json.JSONDecodeError as error:
        raise _SetupError(f"configuration is not JSON: {error.msg}") from error
    if not isinstance(config, dict) or config.get("version") != WRAPPER_CONFIG_VERSION:
        raise _SetupError("configuration version is not supported")
    command = list(argv[2:])
    if not os.path.isabs(command[0]):
        raise _SetupError("the command's executable must be an absolute path")
    return config, command


def _apply_rlimits(value: object) -> None:
    if not isinstance(value, dict):
        raise _SetupError("rlimits are missing")
    if resource is None:
        raise _SetupError("resource limits are not available on this platform")
    _set_limit(resource.RLIMIT_CORE, _integer(value, "core"))
    _set_limit(resource.RLIMIT_FSIZE, _integer(value, "fsize"))
    _set_limit(resource.RLIMIT_CPU, _integer(value, "cpu"))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = _integer(value, "nofile")
    if soft == resource.RLIM_INFINITY or soft > wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def _set_limit(which: int, wanted: int) -> None:
    """Lower both limits to ``wanted``; never raise one that is already lower."""

    assert resource is not None
    soft, hard = resource.getrlimit(which)
    new_hard = wanted if hard == resource.RLIM_INFINITY or hard > wanted else hard
    new_soft = min(wanted, new_hard) if soft == resource.RLIM_INFINITY else min(soft, new_hard)
    resource.setrlimit(which, (new_soft, new_hard))


def _apply_landlock(config: dict[str, object]) -> None:
    abi = _integer(config, "abi")
    rules_value = config.get("rules")
    if not isinstance(rules_value, list) or not rules_value:
        raise _SetupError("rules are missing")
    rules: list[tuple[str, AccessClass]] = []
    for item in rules_value:
        if not isinstance(item, dict):
            raise _SetupError("a rule is not an object")
        path, access, optional = item.get("path"), item.get("access"), item.get("optional")
        if not isinstance(path, str) or not os.path.isabs(path) or not isinstance(optional, bool):
            raise _SetupError("a rule is malformed")
        if not os.path.exists(path):
            if optional:
                continue
            raise _SetupError(f"a required rule path does not exist: {path}")
        rules.append((path, AccessClass(str(access))))
    # This process's own /proc entry, and no other: execve keeps the identifier, so the
    # command can read its own status while ProofCoder's environment stays out of reach.
    rules.append((f"/proc/{os.getpid()}", AccessClass.READ_FILE))
    deny_tcp, scope = config.get("deny_tcp"), config.get("scope")
    if not isinstance(deny_tcp, bool) or not isinstance(scope, bool):
        raise _SetupError("network settings are malformed")
    restrict_self(rules, abi=abi, deny_tcp=deny_tcp, scope=scope)


def _apply_socket_filter(value: object) -> None:
    """Install the socket filter last: after it, nothing here needs a socket."""

    if not isinstance(value, dict):
        raise _SetupError("socket settings are missing")
    restrict, deny_all = value.get("restrict"), value.get("deny_all")
    if not isinstance(restrict, bool) or not isinstance(deny_all, bool):
        raise _SetupError("socket settings are malformed")
    if restrict:
        install_socket_filter(deny_all_sockets=deny_all)
    elif deny_all:
        raise _SetupError("denying every socket needs the socket filter")


def _integer(mapping: dict[str, object], key: str) -> int:
    value = mapping.get(key)
    if type(value) is not int or value < 0:
        raise _SetupError(f"{key} must be a non-negative integer")
    return value


def _fail(reason: str) -> int:
    message = f"{SETUP_FAILED_MARKER} {reason}\n".encode("utf-8", errors="replace")
    with suppress(OSError):
        os.write(2, message)
    return SETUP_FAILED_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
