"""Paper rows.

`papers.authors` is one TEXT column (CLAUDE.md's schema), holding names joined
with "; ". Splitting/joining happens here and nowhere else, so the FTS triggers
can index the column verbatim and the API can still speak in lists.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import Any

AUTHOR_SEP = "; "

_UPDATABLE = frozenset(
    {
        "title",
        "authors",
        "year",
        "venue",
        "source_url",
        "abstract",
        "summary_short",
        "summary_long",
        "status",
    }
)


def join_authors(authors: Sequence[str] | None) -> str | None:
    if not authors:
        return None
    cleaned = [a.strip() for a in authors if a and a.strip()]
    return AUTHOR_SEP.join(cleaned) or None


def split_authors(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def find_by_sha256(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM papers WHERE sha256 = ?", (sha256,)).fetchone()


def find_by_source_url(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM papers WHERE source_url = ? ORDER BY id LIMIT 1", (url,)
    ).fetchone()


def get_paper(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()


def create_paper(conn: sqlite3.Connection, *, sha256: str, source_url: str | None = None) -> int:
    """Insert the row as soon as the bytes are known.

    Everything else arrives later, which is what makes each pipeline step
    independently re-runnable.
    """
    cursor = conn.execute(
        "INSERT INTO papers (sha256, source_url) VALUES (?, ?)", (sha256, source_url)
    )
    return int(cursor.lastrowid)


def get_or_create_by_sha256(
    conn: sqlite3.Connection, sha256: str, *, source_url: str | None = None
) -> tuple[int, bool]:
    """Returns (paper_id, created). Idempotent: re-running ingest for the same
    bytes finds the existing row instead of colliding on the UNIQUE index."""
    existing = find_by_sha256(conn, sha256)
    if existing is not None:
        return int(existing["id"]), False
    try:
        return create_paper(conn, sha256=sha256, source_url=source_url), True
    except sqlite3.IntegrityError:
        existing = find_by_sha256(conn, sha256)
        if existing is None:
            raise
        return int(existing["id"]), False


def update_paper(conn: sqlite3.Connection, paper_id: int, **fields: Any) -> None:
    """Update whitelisted columns, ignoring None (never overwrite known data
    with a blank — arXiv metadata must survive the LLM step)."""
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE and v is not None}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE papers SET {assignments} WHERE id = ?",
        (*updates.values(), paper_id),
    )


def set_full_text(conn: sqlite3.Connection, paper_id: int, full_text: str, n_pages: int) -> None:
    conn.execute(
        """
        INSERT INTO paper_texts (paper_id, full_text, n_pages)
        VALUES (?, ?, ?)
        ON CONFLICT (paper_id) DO UPDATE SET
            full_text = excluded.full_text,
            n_pages = excluded.n_pages,
            extracted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (paper_id, full_text, n_pages),
    )


def get_full_text(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT paper_id, full_text, n_pages FROM paper_texts WHERE paper_id = ?", (paper_id,)
    ).fetchone()


def count_papers(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"])


def list_papers(
    conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.*, t.n_pages
        FROM papers p
        LEFT JOIN paper_texts t ON t.paper_id = p.id
        ORDER BY p.added_at DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
