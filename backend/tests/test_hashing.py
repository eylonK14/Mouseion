"""Content hashing and the dedup it powers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mouseion.services.hashing import is_sha256, sha256_bytes, sha256_file
from mouseion.services.papers import find_by_sha256, get_or_create_by_sha256
from mouseion.services.pdfs import NotAPdfError, looks_like_pdf, pdf_path_for, store_pdf


def test_sha256_bytes_is_stable_and_content_addressed() -> None:
    assert sha256_bytes(b"hello") == sha256_bytes(b"hello")
    assert sha256_bytes(b"hello") != sha256_bytes(b"hellp")
    assert is_sha256(sha256_bytes(b"hello"))


def test_sha256_file_matches_sha256_bytes(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    data = b"%PDF-1.7\n" + b"x" * 5000
    path.write_bytes(data)
    assert sha256_file(path) == sha256_bytes(data)


def test_looks_like_pdf() -> None:
    assert looks_like_pdf(b"%PDF-1.4\nrest")
    assert looks_like_pdf(b"\n  %PDF-1.4")  # leading whitespace tolerated
    assert not looks_like_pdf(b"<!doctype html><html>")
    assert not looks_like_pdf(b"")


def test_store_pdf_is_content_addressed_and_idempotent(
    fixture_pdf_bytes: bytes, settings
) -> None:
    path, digest = store_pdf(fixture_pdf_bytes, settings.pdf_dir)
    assert path == pdf_path_for(settings.pdf_dir, digest)
    assert path.read_bytes() == fixture_pdf_bytes

    # Re-storing the same bytes must not create a second file.
    path_again, digest_again = store_pdf(fixture_pdf_bytes, settings.pdf_dir)
    assert (path_again, digest_again) == (path, digest)
    assert len(list(settings.pdf_dir.glob("*.pdf"))) == 1

    # And no temp files are left behind by the atomic write.
    assert not list(settings.pdf_dir.glob("*.tmp-*"))


def test_store_pdf_rejects_non_pdf(settings) -> None:
    with pytest.raises(NotAPdfError):
        store_pdf(b"<!doctype html><html>nope</html>", settings.pdf_dir)


def test_different_pdfs_get_different_hashes(
    fixture_pdf_bytes: bytes, other_pdf: Path, settings
) -> None:
    _, first = store_pdf(fixture_pdf_bytes, settings.pdf_dir)
    _, second = store_pdf(other_pdf.read_bytes(), settings.pdf_dir)
    assert first != second


def test_get_or_create_by_sha256_dedups(conn: sqlite3.Connection) -> None:
    digest = sha256_bytes(b"%PDF-fake")

    paper_id, created = get_or_create_by_sha256(conn, digest, source_url="https://example.com/a")
    assert created is True

    same_id, created_again = get_or_create_by_sha256(
        conn, digest, source_url="https://example.com/different-url"
    )
    assert created_again is False
    assert same_id == paper_id

    assert conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"] == 1
    # The first source_url wins; a duplicate submit does not rewrite history.
    assert find_by_sha256(conn, digest)["source_url"] == "https://example.com/a"


def test_sha256_unique_constraint_is_enforced_by_the_schema(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO papers (sha256) VALUES ('deadbeef')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO papers (sha256) VALUES ('deadbeef')")
