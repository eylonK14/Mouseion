"""Notes CRUD, nested under the paper they belong to.

Every route is scoped by `paper_id`, so a note id from one paper cannot be
read or written through another paper's URL.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response, status

from mouseion.api.models import NoteIn, NoteListOut, NoteOut
from mouseion.db import get_db
from mouseion.services import notes as notes_repo
from mouseion.services import papers as papers_repo

router = APIRouter(prefix="/api/papers/{paper_id}/notes", tags=["notes"])


def _require_paper(conn: sqlite3.Connection, paper_id: int) -> None:
    if papers_repo.get_paper(conn, paper_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"paper {paper_id} not found")


@router.get("", response_model=NoteListOut)
def list_notes(paper_id: int, conn: sqlite3.Connection = Depends(get_db)) -> NoteListOut:
    _require_paper(conn, paper_id)
    return NoteListOut(items=[NoteOut.from_row(r) for r in notes_repo.list_notes(conn, paper_id)])


@router.post("", response_model=NoteOut, status_code=status.HTTP_201_CREATED)
def create_note(
    paper_id: int, body: NoteIn, conn: sqlite3.Connection = Depends(get_db)
) -> NoteOut:
    """Create a note, usually empty — the editor autosaves into it afterwards."""
    _require_paper(conn, paper_id)
    return NoteOut.from_row(notes_repo.create_note(conn, paper_id, body.content))


@router.get("/{note_id}", response_model=NoteOut)
def read_note(
    paper_id: int, note_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> NoteOut:
    row = notes_repo.get_note(conn, paper_id, note_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"note {note_id} not found")
    return NoteOut.from_row(row)


@router.put("/{note_id}", response_model=NoteOut)
def update_note(
    paper_id: int, note_id: int, body: NoteIn, conn: sqlite3.Connection = Depends(get_db)
) -> NoteOut:
    """Full replacement — this is what the autosaving editor calls."""
    row = notes_repo.update_note(conn, paper_id, note_id, body.content)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"note {note_id} not found")
    return NoteOut.from_row(row)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(
    paper_id: int, note_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> Response:
    if not notes_repo.delete_note(conn, paper_id, note_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"note {note_id} not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
