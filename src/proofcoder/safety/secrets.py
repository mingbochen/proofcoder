"""Central filtering for sensitive paths, environment names, and values."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath

_ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)
_SENSITIVE_ENVIRONMENT_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_SAFE_TOKEN_STATISTIC_NAMES = frozenset(
    {"completion_tokens", "input_tokens", "output_tokens", "prompt_tokens", "total_tokens"}
)
_COMMAND_ENVIRONMENT_DEFAULTS = {
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "NO_COLOR": "1",
    "PAGER": "cat",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "TERM": "dumb",
}

_SENSITIVE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "_netrc",
        "application_default_credentials.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "service-account.json",
        "service_account.json",
    }
)
_SENSITIVE_SUFFIXES = frozenset(
    {
        ".cer",
        ".crt",
        ".der",
        ".jks",
        ".key",
        ".keystore",
        ".p12",
        ".pem",
        ".pfx",
    }
)
_AUTHORIZATION_PATTERN = re.compile(r"(?i)\b(authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,;]+)")
_NAMED_SECRET_PATTERN = re.compile(
    r"(?i)\b([A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)"
    r"[A-Z0-9_.-]*)(\s*[:=]\s*)([^\s,;}]+)"
)
_COMMON_TOKEN_PATTERN = re.compile(r"(?i)\b(?:sk|ds)-[A-Za-z0-9_-]{8,}\b")
_ANSI_CSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_PATTERN = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def is_sensitive_filename(name: str) -> bool:
    """Return whether an exact filename denotes credentials or key material."""

    lowered = name.casefold()
    if lowered == ".env.example":
        return False
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    return lowered in _SENSITIVE_NAMES or Path(lowered).suffix in _SENSITIVE_SUFFIXES


def is_sensitive_path(path: str | PurePath) -> bool:
    """Return whether any meaningful component of a relative path is sensitive."""

    candidate = PurePosixPath(str(path).replace("\\", "/"))
    return any(
        part not in {"", ".", ".."} and is_sensitive_filename(part) for part in candidate.parts
    )


def is_sensitive_environment_name(name: str) -> bool:
    """Return whether an environment variable name resembles a credential."""

    upper_name = name.upper()
    return any(marker in upper_name for marker in _SENSITIVE_ENVIRONMENT_MARKERS)


def is_safe_token_statistic(name: str, value: object) -> bool:
    """Return whether a value is a non-secret structured token-count statistic."""

    return name.casefold() in _SAFE_TOKEN_STATISTIC_NAMES and (
        value is None or (type(value) is int and value >= 0)
    )


def minimal_subprocess_environment(
    environ: Mapping[str, str] | None = None,
    *,
    command_defaults: bool = False,
) -> dict[str, str]:
    """Return only process essentials without reading excluded secret values."""

    source = os.environ if environ is None else environ
    filtered: dict[str, str] = {}
    for name in source:
        upper_name = name.upper()
        if upper_name not in _ALLOWED_ENVIRONMENT_NAMES:
            continue
        if is_sensitive_environment_name(upper_name):
            continue
        filtered[upper_name] = source[name]
    if command_defaults:
        filtered.update(_COMMAND_ENVIRONMENT_DEFAULTS)
    return filtered


def sensitive_environment_values(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return non-empty known secret values for exact comparison and redaction only."""

    source = os.environ if environ is None else environ
    values = {
        source[name] for name in source if is_sensitive_environment_name(name) and source[name]
    }
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def redact_text(text: str, *, sensitive_values: tuple[str, ...] = ()) -> str:
    """Redact known values and common credential assignments from arbitrary text."""

    redacted = _ANSI_OSC_PATTERN.sub("", _ANSI_CSI_PATTERN.sub("", text))
    redacted = "".join(
        character
        for character in redacted
        if character in {"\n", "\t"} or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    for value in sorted(
        (item for item in sensitive_values if item),
        key=lambda item: (-len(item), item),
    ):
        redacted = redacted.replace(value, "[redacted]")
    redacted = _AUTHORIZATION_PATTERN.sub(r"\1[redacted]", redacted)
    redacted = _NAMED_SECRET_PATTERN.sub(_redact_named_secret, redacted)
    return _COMMON_TOKEN_PATTERN.sub("[redacted]", redacted)


def _redact_named_secret(match: re.Match[str]) -> str:
    name, separator, value = match.groups()
    if value.isascii() and value.isdecimal() and is_safe_token_statistic(name, int(value)):
        return match.group(0)
    return f"{name}{separator}[redacted]"
