"""API request/response models (Pydantic v2, per CLAUDE.md conventions)."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from mouseion.services.papers import split_authors
from mouseion.services.taxonomy import TopicRow

PaperStatus = Literal["to_read", "reading", "read", "understood"]


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
