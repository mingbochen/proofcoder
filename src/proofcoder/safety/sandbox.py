"""Operating-system isolation for the commands ``run_command`` starts.

The decision is made once, before a run: which mode the user asked for, what this host
can enforce, and therefore which state every command of the run is in. A command never
discovers mid-run that it will not be isolated after all. When isolation is in force,
each command is started through an execution wrapper that restricts itself and then
replaces itself with the command, and a wrapper that cannot restrict itself runs
nothing. Specification section 10.7 and ADR-0010 set these rules.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from proofcoder.safety.landlock import SCOPE_ABI, TCP_ABI, AccessClass, LandlockProbe
from proofcoder.safety.landlock import probe as probe_landlock

WRAPPER_MODULE = "proofcoder.sandbox_exec"
# A wrapper that fails before exec exits with this code and prints the marker as the
# first line of standard error. The pair, not the code alone, identifies it.
SETUP_FAILED_EXIT_CODE = 125
SETUP_FAILED_MARKER = "proofcoder-sandbox-setup-failed:"
WRAPPER_CONFIG_VERSION = 1

SYSTEM_READ_PATHS = ("/usr", "/lib", "/lib32", "/lib64", "/bin", "/sbin", "/etc")
DEVICE_READ_WRITE_PATHS = ("/dev/null",)
DEVICE_READ_PATHS = ("/dev/zero", "/dev/random", "/dev/urandom")
TEMPORARY_ROOT = Path(".proofcoder/runtime/tmp")
TEMPORARY_VARIABLES = ("TMPDIR", "TMP", "TEMP")

RLIMIT_FSIZE_BYTES = 1024 * 1024 * 1024
RLIMIT_NOFILE_SOFT = 4096
RLIMIT_CPU_MARGIN_SECONDS = 5


class SandboxMode(StrEnum):
    """What the user asked for."""

    AUTO = "auto"
    REQUIRED = "required"
    OFF = "off"


class SandboxStatus(StrEnum):
    """What this run actually gets."""

    ENFORCED = "enforced"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class SandboxSettings:
    """The user's isolation choices. Only the command line may supply them."""

    mode: SandboxMode = SandboxMode.AUTO
    extra_read_paths: tuple[Path, ...] = ()
    allow_network: bool = False


@dataclass(frozen=True, slots=True)
class SandboxState:
    """The isolation every command of one run is in, decided before the run starts."""

    settings: SandboxSettings
    status: SandboxStatus
    abi: int | None
    filesystem: bool
    tcp_restricted: bool
    scoped: bool
    reason: str | None

    @property
    def isolates(self) -> bool:
        """Whether commands go through the wrapper at all."""

        return self.status in {SandboxStatus.ENFORCED, SandboxStatus.PARTIAL}

    @property
    def satisfies_required(self) -> bool:
        """Whether a run in ``required`` mode may start."""

        return self.status is SandboxStatus.ENFORCED


def decide_sandbox(
    settings: SandboxSettings,
    *,
    probe: Callable[[], LandlockProbe] = probe_landlock,
) -> SandboxState:
    """Probe once and settle the state for a whole run."""

    if settings.mode is SandboxMode.OFF:
        return SandboxState(
            settings=settings,
            status=SandboxStatus.OFF,
            abi=None,
            filesystem=False,
            tcp_restricted=False,
            scoped=False,
            reason="isolation was turned off",
        )
    result = probe()
    if result.abi is None:
        unsupported = not sys.platform.startswith("linux")
        return SandboxState(
            settings=settings,
            status=SandboxStatus.UNSUPPORTED if unsupported else SandboxStatus.UNAVAILABLE,
            abi=None,
            filesystem=False,
            tcp_restricted=False,
            scoped=False,
            reason=result.reason,
        )
    abi = result.abi
    tcp_restricted = not settings.allow_network and abi >= TCP_ABI
    network_short = not settings.allow_network and not tcp_restricted
    return SandboxState(
        settings=settings,
        status=SandboxStatus.PARTIAL if network_short else SandboxStatus.ENFORCED,
        abi=abi,
        filesystem=True,
        tcp_restricted=tcp_restricted,
        scoped=abi >= SCOPE_ABI,
        reason=(
            f"TCP cannot be restricted below Landlock ABI {TCP_ABI}; this kernel has {abi}"
            if network_short
            else None
        ),
    )


def sandbox_payload(state: SandboxState) -> dict[str, object]:
    """Build the trace payload for a run's isolation state. Paths are counted, not listed."""

    return {
        "mode": state.settings.mode.value,
        "status": state.status.value,
        "abi": state.abi,
        "filesystem": state.filesystem,
        "tcp_restricted": state.tcp_restricted,
        "scoped": state.scoped,
        "extra_read_paths": len(state.settings.extra_read_paths),
        "reason": state.reason,
    }


def install_root(executable: Path) -> Path:
    """Return the directory an executable was installed under.

    ``.../bin/python3`` belongs to ``...``, where its standard library and a virtual
    environment's ``pyvenv.cfg`` live; anything else belongs to its own directory.
    """

    parent = executable.parent
    return parent.parent if parent.name == "bin" else parent


def wrapper_rules(
    *,
    workspace: Path,
    temporary: Path,
    executable: str,
    settings: SandboxSettings,
) -> list[dict[str, object]]:
    """List what one command may reach. Anything not listed is denied."""

    rules: list[dict[str, object]] = [
        {"path": str(workspace), "access": AccessClass.FULL.value, "optional": False},
        {"path": str(temporary), "access": AccessClass.FULL.value, "optional": False},
    ]
    roots = [Path(item) for item in SYSTEM_READ_PATHS]
    as_given = Path(executable)
    roots.extend(dict.fromkeys([install_root(as_given), install_root(as_given.resolve())]))
    roots.extend(settings.extra_read_paths)
    for root in dict.fromkeys(roots):
        rules.append(
            {"path": str(root), "access": AccessClass.READ_EXECUTE.value, "optional": True}
        )
    for device in DEVICE_READ_WRITE_PATHS:
        rules.append(
            {"path": device, "access": AccessClass.READ_WRITE_FILE.value, "optional": True}
        )
    for device in DEVICE_READ_PATHS:
        rules.append({"path": device, "access": AccessClass.READ_FILE.value, "optional": True})
    return rules


def wrapped_argv(
    execution_argv: Sequence[str],
    *,
    workspace: Path,
    temporary: Path,
    state: SandboxState,
    timeout_seconds: int,
) -> list[str]:
    """Return the argv that starts ``execution_argv`` inside the wrapper.

    The wrapper runs on this interpreter with ``-P`` and ``-E``: nothing from the working
    directory or from ``PYTHON*`` variables may be imported before the restriction is
    in place, because until then the wrapper is exactly as privileged as ProofCoder.
    """

    if state.abi is None:
        raise ValueError("a command can only be wrapped when isolation is in force")
    config = {
        "version": WRAPPER_CONFIG_VERSION,
        "abi": state.abi,
        "rules": wrapper_rules(
            workspace=workspace,
            temporary=temporary,
            executable=execution_argv[0],
            settings=state.settings,
        ),
        "deny_tcp": state.tcp_restricted,
        "scope": state.scoped,
        "rlimits": {
            "core": 0,
            "fsize": RLIMIT_FSIZE_BYTES,
            "nofile": RLIMIT_NOFILE_SOFT,
            "cpu": timeout_seconds + RLIMIT_CPU_MARGIN_SECONDS,
        },
    }
    return [
        sys.executable,
        "-P",
        "-E",
        "-m",
        WRAPPER_MODULE,
        json.dumps(config, separators=(",", ":"), sort_keys=True),
        "--",
        *execution_argv,
    ]


def is_setup_failure(exit_code: int | None, stderr: str) -> bool:
    """Recognize the wrapper's own failure, as opposed to the command's."""

    return exit_code == SETUP_FAILED_EXIT_CODE and stderr.startswith(SETUP_FAILED_MARKER)


def create_private_temporary(workspace: Path) -> Path:
    """Create one command's private temporary directory under the runtime directory."""

    root = workspace / TEMPORARY_ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / os.urandom(16).hex()
    directory.mkdir(mode=0o700)
    return directory
