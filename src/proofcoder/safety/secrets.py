"""Central, case-insensitive identification of sensitive workspace paths."""

from __future__ import annotations

from pathlib import Path, PurePath, PurePosixPath

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
