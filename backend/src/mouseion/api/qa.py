"""Authenticated streaming adapters for grounded collection and paper QA."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from mouseion.api.search import to_hits
from mouseion.db import get_db
from mouseion.services.llm import LLMClient, LLMError, LLMTask, get_llm_client
from mouseion.services.qa import (
    QANotFoundError,
    PreparedQA,
    citation_warnings,
    prepare_collection_qa,
    prepare_paper_qa,
    record_qa_log,
    resolve_paper_from_text,
    synthesis_messages,
)
from mouseion.services.search import SearchQuery, SearchResult

router = APIRouter(prefix="/api/qa", tags=["qa"])


class QAMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class QAIn(BaseModel):
    question: str | None = Field(default=None, max_length=20_000)
    messages: list[QAMessage] = Field(default_factory=list, max_length=100)
    topic: int | str | None = None

    @model_validator(mode="after")
    def has_a_question(self) -> QAIn:
        if self.question is not None and self.question.strip():
            self.question = self.question.strip()
            return self
        if any(message.role == "user" and message.content.strip() for message in self.messages):
            return self
        raise ValueError("provide question or at least one user message")

    def split_conversation(self) -> tuple[str, list[dict[str, str]]]:
        messages = [message.model_dump() for message in self.messages]
        if self.question:
            return self.question, messages
        for index in range(len(messages) - 1, -1, -1):
            if messages[index]["role"] == "user":
                return messages[index]["content"].strip(), messages[:index]
        raise ValueError("conversation has no user message")


class PaperResolveIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


def _sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _response_metadata(
    conn: sqlite3.Connection, prepared: PreparedQA
) -> dict[str, object]:
    """Attach the frozen SearchHit payload with one paper query + one topic query."""
    metadata = prepared.metadata()
    consulted = metadata["consulted_papers"]
    if not isinstance(consulted, list) or not consulted:
        return metadata
    ids = [material.paper_id for material in prepared.materials]
    score_by_id = {material.paper_id: material.score for material in prepared.materials}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT p.*, tx.n_pages, NULL AS score, NULL AS snippet
        FROM papers p
        LEFT JOIN paper_texts tx ON tx.paper_id = p.id
        WHERE p.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    rows.sort(key=lambda row: ids.index(int(row["id"])))
    # sqlite.Row cannot be copied with one changed field, so to_hits gets its
    # usual rows and the hybrid score is applied to the validated model after
    # its batched topic lookup.
    hits = to_hits(
        conn,
        SearchResult(rows=list(rows), total=len(rows), query=SearchQuery()),
    )
    hit_by_id = {hit.paper.id: hit for hit in hits}
    for entry in consulted:
        paper_id = int(entry["id"])
        hit = hit_by_id.get(paper_id)
        if hit is not None:
            hit.score = score_by_id[paper_id]
            entry["hit"] = hit.model_dump(mode="json")
    return metadata


async def _answer_stream(
    conn: sqlite3.Connection,
    client: LLMClient,
    prepared: PreparedQA,
    *,
    started_at: float,
    metadata: dict[str, object],
) -> AsyncIterator[str]:
    answer_parts: list[str] = []
    issues: list[str] = []
    logged = False
    stream_error: str | None = None
    yield _sse("metadata", metadata)
    try:
        if not prepared.materials:
            token = "The answer is not in your library."
            answer_parts.append(token)
            yield _sse("token", {"text": token})
        else:
            stream = client.stream_text(task=LLMTask.QA, messages=synthesis_messages(prepared))
            try:
                async for token in stream:
                    answer_parts.append(token)
                    yield _sse("token", {"text": token})
            finally:
                prepared.metrics.add(stream.metrics)

        issues = citation_warnings("".join(answer_parts), prepared.materials)
        log_id = record_qa_log(
            conn, prepared, started_at=started_at, citation_issues=issues
        )
        logged = True
        done = {
            **metadata,
            "citation_warnings": issues,
            "citations_valid": not issues,
            "qa_log_id": log_id,
        }
        yield _sse("done", done)
    except LLMError as exc:
        stream_error = str(exc)
        if not logged:
            log_id = record_qa_log(
                conn,
                prepared,
                started_at=started_at,
                citation_issues=issues,
                error=stream_error,
            )
            logged = True
        yield _sse("error", {"detail": stream_error, "qa_log_id": log_id})
    finally:
        if not logged:
            record_qa_log(
                conn,
                prepared,
                started_at=started_at,
                citation_issues=issues,
                error=stream_error or "stream ended before completion",
            )


def _response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/collection")
async def collection_qa(
    payload: QAIn,
    conn: sqlite3.Connection = Depends(get_db),
    client: LLMClient = Depends(get_llm_client),
) -> StreamingResponse:
    started_at = perf_counter()
    question, history = payload.split_conversation()
    try:
        prepared = await prepare_collection_qa(
            conn,
            question,
            history,
            client=client,
            topic_scope=payload.topic,
        )
    except QANotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _response(
        _answer_stream(
            conn,
            client,
            prepared,
            started_at=started_at,
            metadata=_response_metadata(conn, prepared),
        )
    )


@router.post("/paper/resolve")
def resolve_paper(
    payload: PaperResolveIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, object]:
    resolution = resolve_paper_from_text(conn, payload.text)
    return {
        "locked": (
            {
                "id": resolution.locked.id,
                "title": resolution.locked.title,
                "score": resolution.locked.score,
            }
            if resolution.locked
            else None
        ),
        "matches": [
            {"id": match.id, "title": match.title, "score": match.score}
            for match in resolution.matches
        ],
    }


@router.post("/paper/{paper_id}")
async def paper_qa(
    paper_id: int,
    payload: QAIn,
    conn: sqlite3.Connection = Depends(get_db),
    client: LLMClient = Depends(get_llm_client),
) -> StreamingResponse:
    started_at = perf_counter()
    question, history = payload.split_conversation()
    try:
        prepared = await prepare_paper_qa(
            conn, paper_id, question, history, client=client
        )
    except QANotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _response(
        _answer_stream(
            conn,
            client,
            prepared,
            started_at=started_at,
            metadata=_response_metadata(conn, prepared),
        )
    )
