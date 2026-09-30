"""Where attachment bytes live: a local folder, referenced from the database.

The ``attachments`` row keeps only metadata plus ``blob_key``; the bytes sit
at ``<ATTACHMENT_DIR>/<sha256>/<filename>``. Keys are content-addressed, so a
file that arrives in two mails is stored once and a retried write is
idempotent. Pure Python: no services, no network.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from .config import Settings

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AttachmentStoreError(RuntimeError):
    """The store could not save or return an attachment."""


class FileSystemStore:
    """One file per distinct attachment under ``root/<sha256>/<filename>``.

    Writes go to a temporary name and are renamed into place, so a crash
    mid-write never leaves a half file behind. An existing file with the
    same key is left alone: same hash, same bytes.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def put(self, *, sha256: str, filename: str, data: bytes) -> str:
        """Store ``data`` and return its blob key."""
        key = blob_key(sha256, filename)
        target = self._path_for(key)
        if not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".part")
                tmp.write_bytes(data)
                os.replace(tmp, target)
            except OSError as exc:
                raise AttachmentStoreError(f"could not write {key!r} under {self.root}: {exc}") from exc
        return key

    def get(self, key: str) -> bytes:
        path = self._path_for(key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise AttachmentStoreError(f"file {key!r} is missing from {self.root}") from None
        except OSError as exc:
            raise AttachmentStoreError(f"could not read {key!r}: {exc}") from exc

    def exists(self, key: str) -> bool:
        try:
            return self._path_for(key).exists()
        except AttachmentStoreError:
            return False

    def describe(self) -> str:
        return f"local files under {self.root}"

    def _path_for(self, key: str) -> Path:
        """Resolve a stored key, refusing anything that escapes the root."""
        parts = PurePosixPath(key).parts
        if (
            len(parts) != 2
            or not _SHA256_RE.match(parts[0])
            or parts[1] in ("", ".", "..")
            or "/" in parts[1]
            or "\\" in parts[1]
        ):
            raise AttachmentStoreError(f"invalid attachment reference {key!r}")
        return self.root / parts[0] / parts[1]


def blob_key(sha256: str, filename: str) -> str:
    return f"{sha256}/{filename}"


def build_store(settings: Settings) -> FileSystemStore:
    return FileSystemStore(settings.attachment_dir)
