"""Content addressing.

The sha256 of the PDF bytes is the paper's identity: it names the file on disk
(`data/pdfs/<sha256>.pdf`), it is the UNIQUE column on `papers`, and it is what
makes re-uploading the same paper a no-op instead of a second row.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())
