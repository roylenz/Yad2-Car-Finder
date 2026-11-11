"""Photo storage. Files are content-addressed (sha256), so a re-posted photo is stored once.

`PhotoStore` is the seam for other backends (e.g. an S3/R2 bucket for offsite copies).
"""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from typing import Protocol

from storage.db import data_dir


class PhotoStore(Protocol):
    async def save(self, data: bytes, extension: str) -> tuple[str, str]:
        """Store the bytes; return (sha256, key)."""

    def path(self, key: str) -> Path:
        """Local file for a stored key (processors and the dashboard read from it)."""


class LocalPhotoStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("PHOTO_DIR") or data_dir() / "photos")

    async def save(self, data: bytes, extension: str) -> tuple[str, str]:
        sha256 = hashlib.sha256(data).hexdigest()
        key = f"{sha256[:2]}/{sha256[2:4]}/{sha256}{extension}"
        await asyncio.to_thread(self._write, self.path(key), data)
        return sha256, key

    def path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Photo key escapes the store: {key}")
        return path

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temp file per write: the same photo can arrive for two ads at once.
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
