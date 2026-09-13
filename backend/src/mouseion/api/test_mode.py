"""Authenticated transport adapters for the Phase 4 examiner engine."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from mouseion.db import get_db
from mouseion.services import papers as papers_repo
from mouseion.services.examiner import (
    ExaminerConflictError,
    ExaminerNotFoundError,
    create_session,
    ensure_session_current,
    get_session,
    list_sessions,
    session_payload,
    advance_session,
)
from mouseion.services.llm import LLMClient, LLMError, get_llm_client
from mouseion.services.schemas import ExamVerdict

router = APIRouter(prefix="/api/test", tags=["test mode"])


class TestTurnIn(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)

    @field_validator("answer")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        answer = value.strip()
        if not answer:
            raise ValueError("answer must not be blank")
        return answer


class TranscriptTurnOut(BaseModel):
    role: Literal["user", "assistant"]
    kind: str
    content: str
    created_at: str
    section: str | None = None
    focus: str | None = None
    targets_gap: bool | None = None


class RereadTargetOut(BaseModel):
    section: str
    start_page: int | None = None
    end_page: int | None = None


class TestSessionOut(BaseModel):
    id: int
    paper_id: int
    phase: Literal["explain", "probe", "verdict"]
    turn_count: int
    opening_question: str
    current_question: str | None = None
    transcript: list[TranscriptTurnOut] = Field(default_factory=list)
    verdict: ExamVerdict | None = None
    reread_targets: list[RereadTargetOut] = Field(default_factory=list)
    created_at: str
    updated_at: str
    expires_at: str | None = None
    completed_at: str | None = None


class TestSessionListOut(BaseModel):
    items: list[TestSessionOut]


def _out(payload: dict[str, object]) -> TestSessionOut:
    return TestSessionOut.model_validate(payload)


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, ExaminerNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ExaminerConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, LLMError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise exc


def _sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/{paper_id}/start", response_model=TestSessionOut)
def start_test(
    paper_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> TestSessionOut:
    try:
        session = create_session(conn, paper_id)
    except (ExaminerNotFoundError, ExaminerConflictError) as exc:
        _raise_service_error(exc)
    return _out(session_payload(conn, session))


# Static prefixes are declared before the dynamic GET route so Starlette never
# interprets "paper" as a session id.
@router.get("/paper/{paper_id}/sessions", response_model=TestSessionListOut)
def paper_sessions(
    paper_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> TestSessionListOut:
    if papers_repo.get_paper(conn, paper_id) is None:
        raise HTTPException(status_code=404, detail=f"paper {paper_id} not found")
    return TestSessionListOut(
        items=[
            _out(session_payload(conn, session))
            for session in list_sessions(conn, paper_id)
        ]
    )


@router.get("/{session_id}", response_model=TestSessionOut)
async def reload_test(
    session_id: int,
    conn: sqlite3.Connection = Depends(get_db),
    client: LLMClient = Depends(get_llm_client),
) -> TestSessionOut:
    try:
        session = await ensure_session_current(conn, session_id, client=client)
    except (ExaminerNotFoundError, ExaminerConflictError, LLMError) as exc:
        _raise_service_error(exc)
    return _out(session_payload(conn, session))


async def _turn_events(
    conn: sqlite3.Connection,
    client: LLMClient,
    session_id: int,
    answer: str,
) -> AsyncIterator[str]:
    session = get_session(conn, session_id)
    yield _sse(
        "metadata",
        {"session_id": session.id, "paper_id": session.paper_id, "phase": session.phase.value},
    )
    try:
        result = await advance_session(
            conn, session_id, answer, client=client
        )
        if result.assistant_text:
            yield _sse("token", {"text": result.assistant_text})
        if result.verdict is not None:
            yield _sse("verdict", result.verdict.model_dump(mode="json"))
        yield _sse(
            "done", session_payload(conn, result.session, include_transcript=False)
        )
    except (ExaminerNotFoundError, ExaminerConflictError, LLMError) as exc:
        yield _sse("error", {"detail": str(exc)})


@router.post("/{session_id}/turn")
def test_turn(
    session_id: int,
    payload: TestTurnIn,
    conn: sqlite3.Connection = Depends(get_db),
    client: LLMClient = Depends(get_llm_client),
) -> StreamingResponse:
    # Fail a bad id before response headers are committed. Model/grounding
    # failures after streaming begins travel as readable SSE error events.
    try:
        get_session(conn, session_id)
    except ExaminerNotFoundError as exc:
        _raise_service_error(exc)
    return StreamingResponse(
        _turn_events(conn, client, session_id, payload.answer),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
