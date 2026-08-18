"""Search and filtered browse.

`GET /api/search` is the one endpoint the library view talks to; the Phase 1
`GET /api/papers` list stays for scripts and compatibility. The response shape
here is frozen for Phase 3 — see `SearchHit`.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from mouseion.api.models import (
    BlankableInt,
    BlankableStatus,
    PaperOut,
    PaperStatus,
    SearchHit,
    SearchOut,
    SearchQueryOut,
)
from mouseion.db import get_db
from mouseion.services.search import (
    SearchQuery,
    SearchResult,
    highlight,
    resolve_sort,
    search_papers,
)
from mouseion.services.taxonomy import topics_for_papers

router = APIRouter(prefix="/api", tags=["search"])


def build_query(
    q: str | None,
    topic_id: int | None,
    status: PaperStatus | None,
    year_from: int | None,
    year_to: int | None,
    sort: str | None,
    limit: int,
    offset: int,
) -> SearchQuery:
    """Normalise raw query-string values into a SearchQuery.

    Shared with the HTMX fragment routes so the two surfaces cannot drift in
    how they interpret a filter.
    """
    text = (q or "").strip() or None
    return SearchQuery(
        q=text,
        topic_id=topic_id,
        status=status,
        year_from=year_from,
        year_to=year_to,
        sort=resolve_sort(sort, has_query=text is not None),
        limit=limit,
        offset=offset,
    )


def to_hits(conn: sqlite3.Connection, result: SearchResult) -> list[SearchHit]:
    """Rows → hits, with topics fetched in one batched query (never N+1)."""
    rows = result.rows
    topics = topics_for_papers(conn, [row["id"] for row in rows])
    return [
        SearchHit(
            paper=PaperOut.from_row(row, topics.get(row["id"], [])),
            score=row["score"],
            snippet=highlight(row["snippet"]),
        )
        for row in rows
    ]


def run_search(conn: sqlite3.Connection, query: SearchQuery) -> SearchOut:
    result = search_papers(conn, query)
    return SearchOut(
        items=to_hits(conn, result),
        total=result.total,
        limit=query.limit,
        offset=query.offset,
        query=SearchQueryOut(
            q=query.q,
            topic_id=query.topic_id,
            status=query.status,  # type: ignore[arg-type]
            year_from=query.year_from,
            year_to=query.year_to,
            sort=query.sort,
        ),
    )


@router.get("/search", response_model=SearchOut)
def search(
    conn: sqlite3.Connection = Depends(get_db),
    # Annotated[...] rather than `= Query(...)`: with the default-value spelling
    # FastAPI drops the BeforeValidator that turns "" into None, and the filter
    # bar submits empty strings for every control the user has not touched.
    q: Annotated[str | None, Query(description="FTS5 search text; empty = browse")] = None,
    topic_id: Annotated[
        BlankableInt, Query(description="Filter to this topic AND all of its descendants")
    ] = None,
    status: Annotated[BlankableStatus, Query()] = None,
    year_from: Annotated[BlankableInt, Query()] = None,
    year_to: Annotated[BlankableInt, Query()] = None,
    sort: Annotated[
        str | None, Query(description="relevance | added_at | year (default: relevance if q)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchOut:
    return run_search(
        conn, build_query(q, topic_id, status, year_from, year_to, sort, limit, offset)
    )
