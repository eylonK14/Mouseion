"""API request/response models (Pydantic v2, per CLAUDE.md conventions)."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator

from mouseion.services.papers import split_authors
from mouseion.services.taxonomy import TopicNode, TopicRow

PaperStatus = Literal["to_read", "reading", "read", "understood"]


def _blank_to_none(value: Any) -> Any:
    """Treat an empty query-string value as "not supplied".

    An HTML form submits every control it contains, so an untouched `<select>`
    or number field arrives as `?status=&year_from=`. Without this the filter
    bar 422s the moment it loads, and the JSON API is stricter than any browser
    can be. Whitespace-only is the same thing.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


#: An optional int that also accepts "" (see `_blank_to_none`).
BlankableInt = Annotated[int | None, BeforeValidator(_blank_to_none)]
#: An optional reading status that also accepts "".
BlankableStatus = Annotated[PaperStatus | None, BeforeValidator(_blank_to_none)]


class TopicOut(BaseModel):
    id: int
    name: str
    parent_id: int | None = None

    @classmethod
    def from_row(cls, topic: TopicRow) -> TopicOut:
        return cls(id=topic.id, name=topic.name, parent_id=topic.parent_id)


class PaperOut(BaseModel):
    id: int
    sha256: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    source_url: str | None = None
    abstract: str | None = None
    summary_short: str | None = None
    summary_long: str | None = None
    status: PaperStatus = "to_read"
    added_at: str
    n_pages: int | None = None
    topics: list[TopicOut] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row, topics: list[TopicRow] | None = None) -> PaperOut:
        keys = row.keys()
        return cls(
            id=row["id"],
            sha256=row["sha256"],
            title=row["title"],
            authors=split_authors(row["authors"]),
            year=row["year"],
            venue=row["venue"],
            source_url=row["source_url"],
            abstract=row["abstract"],
            summary_short=row["summary_short"],
            summary_long=row["summary_long"],
            status=row["status"],
            added_at=row["added_at"],
            n_pages=row["n_pages"] if "n_pages" in keys else None,
            topics=[TopicOut.from_row(t) for t in (topics or [])],
        )


class PaperListOut(BaseModel):
    items: list[PaperOut]
    total: int
    limit: int
    offset: int


class PaperTopicIn(BaseModel):
    """Tag a paper with a topic that already exists in the taxonomy."""

    topic_id: int


class PaperPatchIn(BaseModel):
    """Reading-workflow updates.

    Only `status` is writable. Bibliographic fields come from arXiv or the
    ingest model and are corrected by re-ingesting, not by hand-editing rows.
    """

    status: PaperStatus


# --------------------------------------------------------------------- search
class SearchHit(BaseModel):
    """One result. `paper` is the same object `/api/papers` returns.

    Kept as a wrapper rather than extra fields on PaperOut so Phase 3 can hand
    the identical structure to the "papers consulted" list, with `score`
    carrying retrieval confidence instead of BM25.
    """

    paper: PaperOut
    # Higher is better; None when browsing without a query. Derived from
    # bm25(), so it is comparable within one result set, not across queries.
    score: float | None = None
    # HTML-escaped, with <mark> around the matched terms. Safe to inject.
    snippet: str | None = None


class SearchQueryOut(BaseModel):
    """The effective query, echoed back — `sort` may differ from what was asked
    for (relevance falls back to added_at when there is no query text)."""

    q: str | None = None
    topic_id: int | None = None
    status: PaperStatus | None = None
    year_from: int | None = None
    year_to: int | None = None
    sort: str = "added_at"


class SearchOut(BaseModel):
    items: list[SearchHit]
    total: int
    limit: int
    offset: int
    query: SearchQueryOut


# --------------------------------------------------------------------- topics
class TopicNodeOut(BaseModel):
    id: int
    name: str
    parent_id: int | None = None
    #: papers tagged with this topic exactly
    paper_count: int = 0
    #: papers tagged with this topic or anything below it, each counted once
    total_count: int = 0
    children: list[TopicNodeOut] = Field(default_factory=list)

    @classmethod
    def from_node(cls, node: TopicNode) -> TopicNodeOut:
        return cls(
            id=node.id,
            name=node.name,
            parent_id=node.parent_id,
            paper_count=node.paper_count,
            total_count=node.total_count,
            children=[cls.from_node(child) for child in node.children],
        )


class TopicTreeOut(BaseModel):
    items: list[TopicNodeOut]
    total_topics: int


class TopicCreateIn(BaseModel):
    name: str
    parent_id: int | None = None


class TopicPatchIn(BaseModel):
    """Rename and/or re-parent.

    `parent_id` is tri-state: absent leaves the parent alone, `null` promotes
    the topic to a root, an int moves it. Routes must read
    `model_fields_set` to tell the first two apart.
    """

    name: str | None = None
    parent_id: int | None = None


class TopicMergeIn(BaseModel):
    """Merge this topic into `into_topic_id`; this topic then ceases to exist."""

    into_topic_id: int


class TopicMergeOut(BaseModel):
    target: TopicOut
    papers_relinked: int
    children_moved: int


class TopicSplitIn(BaseModel):
    name: str
    paper_ids: list[int]


class TopicSplitOut(BaseModel):
    source: TopicOut
    created: TopicOut
    papers_moved: int


class TopicDeleteOut(BaseModel):
    deleted: TopicOut
    paper_links_removed: int
    children_promoted: int


# ---------------------------------------------------------------------- notes
class NoteOut(BaseModel):
    id: int
    paper_id: int
    content: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> NoteOut:
        return cls(
            id=row["id"],
            paper_id=row["paper_id"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class NoteListOut(BaseModel):
    items: list[NoteOut]


class NoteIn(BaseModel):
    content: str = ""


class JobOut(BaseModel):
    id: str
    kind: Literal["upload", "url"]
    state: str
    error: str | None = None
    paper_id: int | None = None
    sha256: str | None = None
    duplicate: bool = False
    attempts: int = 0
    completed_steps: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row, progress: dict[str, Any] | None = None) -> JobOut:
        return cls(
            id=row["id"],
            kind=row["kind"],
            state=row["state"],
            error=row["error"],
            paper_id=row["paper_id"],
            sha256=row["sha256"],
            duplicate=bool(row["duplicate"]),
            attempts=row["attempts"],
            completed_steps=list((progress or {}).get("steps", [])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class IngestUrlIn(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        url = (value or "").strip()
        if not url:
            raise ValueError("url must not be empty")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return url


class IngestAccepted(BaseModel):
    """What POST /api/papers returns immediately (CLAUDE.md: ingest is async)."""

    job_id: str
    duplicate: bool = False
    paper_id: int | None = None


class HealthOut(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    sqlite_vec: bool
    tree_indexer: str
