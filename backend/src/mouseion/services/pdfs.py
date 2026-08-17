"""PDF storage and text extraction (PyMuPDF).

Storage is content-addressed and idempotent: writing the same bytes twice is a
no-op, which is half of what makes the ingest pipeline safe to re-run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from mouseion.services.hashing import sha256_bytes

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


class NotAPdfError(ValueError):
    """Raised when downloaded/uploaded bytes are not a PDF."""


@dataclass(slots=True)
class ExtractedText:
    full_text: str
    n_pages: int
    page_texts: list[str] = field(default_factory=list)

    def head(self, n_pages: int) -> str:
        """Text of the first n pages — what non-arXiv metadata extraction sees."""
        return "\n\n".join(self.page_texts[:n_pages]).strip()


def looks_like_pdf(data: bytes) -> bool:
    # Some servers prepend a BOM or stray whitespace before %PDF-.
    return data[:1024].lstrip()[: len(PDF_MAGIC)] == PDF_MAGIC


def pdf_path_for(pdf_dir: Path, sha256: str) -> Path:
    return pdf_dir / f"{sha256}.pdf"


def store_pdf(data: bytes, pdf_dir: Path, sha256: str | None = None) -> tuple[Path, str]:
    """Write bytes to data/pdfs/<sha256>.pdf. Idempotent.

    The write goes to a temp file in the same directory and is then renamed, so
    a crash mid-write can never leave a truncated file sitting at the path that
    the hash promises is complete.
    """
    if not looks_like_pdf(data):
        raise NotAPdfError("content is not a PDF (missing %PDF- header)")

    digest = sha256 or sha256_bytes(data)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    target = pdf_path_for(pdf_dir, digest)
    if target.exists() and target.stat().st_size == len(data):
        return target, digest

    tmp = target.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target, digest


def extract_text(path: Path | str) -> ExtractedText:
    """Extract per-page text. Pages that fail are kept as empty strings so page
    numbers stay aligned with the document."""
    page_texts: list[str] = []
    with pymupdf.open(str(path)) as doc:
        for page in doc:
            try:
                page_texts.append(page.get_text("text") or "")
            except Exception as exc:  # noqa: BLE001 - one bad page must not kill ingest
                log.warning("page %s of %s failed to extract: %s", page.number, path, exc)
                page_texts.append("")
        n_pages = doc.page_count
    return ExtractedText(
        full_text="\n\n".join(page_texts).strip(),
        n_pages=n_pages,
        page_texts=page_texts,
    )


@dataclass(slots=True)
class OutlineEntry:
    level: int
    title: str
    page: int


def extract_outline(path: Path | str) -> list[OutlineEntry]:
    """The PDF's embedded bookmarks, if it has any. Used by the heuristic tree
    indexer as the cheapest possible source of document structure."""
    with pymupdf.open(str(path)) as doc:
        toc = doc.get_toc(simple=True) or []
    return [
        OutlineEntry(level=int(level), title=str(title).strip(), page=int(page))
        for level, title, page in toc
        if str(title).strip()
    ]
