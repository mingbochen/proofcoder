"""Low-level helpers for staged, atomic workspace writes."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Identity and content captured before preparing a replacement."""

    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    digest: bytes

    @classmethod
    def capture(cls, metadata: os.stat_result, content: bytes) -> FileSnapshot:
        """Build a snapshot from one stat result and the bytes it describes."""

        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            mode=metadata.st_mode,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
            digest=hashlib.sha256(content).digest(),
        )

    def matches_metadata(self, metadata: os.stat_result) -> bool:
        """Return whether *metadata* still identifies the captured file state."""

        return (
            self.device == metadata.st_dev
            and self.inode == metadata.st_ino
            and self.mode == metadata.st_mode
            and self.size == metadata.st_size
            and self.modified_ns == metadata.st_mtime_ns
            and self.changed_ns == metadata.st_ctime_ns
        )


def stage_temporary_file(
    target: Path,
    content: bytes,
    *,
    mode: int | None = None,
) -> Path:
    """Durably stage *content* in a temporary file beside *target*.

    The returned file is owned by the caller, which must either commit or remove
    it. If staging is interrupted or fails, this function removes the partial
    temporary file before propagating the original exception.
    """

    descriptor: int | None = None
    temporary: Path | None = None
    complete = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.proofcoder-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            written = stream.write(content)
            if written != len(content):
                raise OSError(
                    f"Short write while staging {target.name}: "
                    f"expected {len(content)} bytes, wrote {written}."
                )
            stream.flush()
            os.fsync(stream.fileno())

        if mode is not None:
            os.chmod(temporary, stat.S_IMODE(mode))

        complete = True
        return temporary
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if not complete and temporary is not None:
            discard_temporary_file(temporary)


def commit_new_file(temporary: Path, target: Path) -> None:
    """Atomically publish *temporary* without overwriting *target*.

    A same-filesystem hard link gives the operation create-if-absent semantics.
    The caller remains responsible for removing the temporary link afterward.
    """

    os.link(temporary, target)


def commit_replacement(temporary: Path, target: Path) -> None:
    """Atomically replace *target* with the staged file."""

    os.replace(temporary, target)


def snapshot_still_matches(path: Path, snapshot: FileSnapshot) -> bool:
    """Check that *path* still has the captured identity and content."""

    try:
        if path.is_symlink():
            return False
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or not snapshot.matches_metadata(before):
            return False
        current_content = path.read_bytes()
        after = path.stat()
    except OSError:
        return False

    return (
        snapshot.matches_metadata(after)
        and hashlib.sha256(current_content).digest() == snapshot.digest
    )


def discard_temporary_file(path: Path) -> None:
    """Best-effort removal of an uncommitted staging file."""

    with suppress(OSError):
        path.unlink()
