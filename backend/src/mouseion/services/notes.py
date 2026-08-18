"""Reading notes.

Markdown text, one row per note, many notes per paper. Rendering is the
frontend's job — the backend stores the source and never interprets it.

Notes are deliberately **not** in the FTS index. They are the reader's own
words, and mixing them into `papers_fts` would let a note about a paper
outrank the paper itself for the terms the reader used. If that turns out to
be wanted, it is a new FTS column plus triggers in a new migration, not a
change here.
"""

from __future__ import annotations

import sqlite3

_TOUCH = "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def list_notes(conn: sqlite3.Connection, paper_id: int) -> list[sqlite3.Row]:
    """Oldest first: a notes column reads top-to-bottom like a lab notebook."""
    return conn.execute(
        "SELECT * FROM notes WHERE paper_id = ? ORDER BY created_at, id", (paper_id,)
    ).fetchall()


def get_note(conn: sqlite3.Connection, paper_id: int, note_id: int) -> sqlite3.Row | None:
    """Scoped to the paper, so a mismatched pair is a 404 rather than a leak."""
    return conn.execute(
        "SELECT * FROM notes WHERE id = ? AND paper_id = ?", (note_id, paper_id)
    ).fetchone()


def create_note(conn: sqlite3.Connection, paper_id: int, content: str = "") -> sqlite3.Row:
    cursor = conn.execute(
        "INSERT INTO notes (paper_id, content) VALUES (?, ?)", (paper_id, content)
    )
    row = get_note(conn, paper_id, int(cursor.lastrowid))
    assert row is not None
    return row


def update_note(
    conn: sqlite3.Connection, paper_id: int, note_id: int, content: str
) -> sqlite3.Row | None:
    conn.execute(
        f"UPDATE notes SET content = ?, {_TOUCH} WHERE id = ? AND paper_id = ?",
        (content, note_id, paper_id),
    )
    return get_note(conn, paper_id, note_id)


def delete_note(conn: sqlite3.Connection, paper_id: int, note_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM notes WHERE id = ? AND paper_id = ?", (note_id, paper_id)
    )
    return cursor.rowcount > 0
