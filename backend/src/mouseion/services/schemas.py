"""Pydantic models for LLM structured output.

These are the contract for the ONE combined ingest call (metadata + topics +
both summaries). Everything the model returns is validated here; on failure the
client retries exactly once with the validation error fed back (CLAUDE.md).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TOPIC_NAME_LEN = 60
MAX_TOPICS_PER_PAPER = 6


class TopicAssignment(BaseModel):
    """One topic decision: pick an existing topic, or propose exactly one new one.

    CLAUDE.md models this as `{existing_topic_id}` OR `{new_topic_name, parent}`.
    It is a flat object with an exactly-one-of validator rather than a union
    because strict JSON-schema modes across providers handle `anyOf` at the top
    of an array item inconsistently — a flat shape validates identically and
    survives more models.
    """

    existing_topic_id: int | None = Field(
        default=None, description="id of a topic from the taxonomy shown in the prompt"
    )
    new_topic_name: str | None = Field(
        default=None, description="name of a new topic to create, only if nothing existing fits"
    )
    parent: int | None = Field(
        default=None,
        description="id of the existing topic the new topic belongs under; null for a root topic",
    )

    @field_validator("new_topic_name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = re.sub(r"\s+", " ", value).strip().strip("#,.;:/\\")
        if not name:
            return None
        if len(name) > MAX_TOPIC_NAME_LEN:
            raise ValueError(f"topic name longer than {MAX_TOPIC_NAME_LEN} characters: {name!r}")
        if not re.search(r"[A-Za-z]", name):
            raise ValueError(f"topic name must contain letters: {name!r}")
        return name

    @model_validator(mode="after")
    def _exactly_one(self) -> TopicAssignment:
        picked = self.existing_topic_id is not None
        proposed = self.new_topic_name is not None
        if picked and proposed:
            raise ValueError(
                "set either existing_topic_id or new_topic_name, not both "
                f"(got id={self.existing_topic_id}, name={self.new_topic_name!r})"
            )
        if not picked and not proposed:
            raise ValueError("each topic needs either existing_topic_id or new_topic_name")
        if picked and self.parent is not None:
            # A parent only means something for a proposal; silently dropping it
            # is friendlier than failing the whole call over a harmless extra.
            self.parent = None
        return self

    @property
    def is_proposal(self) -> bool:
        return self.new_topic_name is not None


class IngestMetadata(BaseModel):
    """Bibliographic fields. Only asked for when not already known — arXiv API
    data always wins (CLAUDE.md)."""

    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    venue: str | None = None
    # Non-arXiv PDFs have no metadata source but the document itself, so the
    # abstract is read off the first pages by this same call (CLAUDE.md 4b).
    # For arXiv papers it is already known and is never requested.
    abstract: str | None = None

    @field_validator("title", "venue", "abstract")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"\s+", " ", value).strip()
        return cleaned or None

    @field_validator("authors")
    @classmethod
    def _tidy_authors(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        authors = [re.sub(r"\s+", " ", a).strip() for a in value]
        return [a for a in authors if a] or None

    @field_validator("year")
    @classmethod
    def _sane_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 1800 <= value <= 2200:
            raise ValueError(f"year out of range: {value}")
        return value


class IngestExtraction(BaseModel):
    """Full payload of the combined ingest call."""

    metadata: IngestMetadata = Field(default_factory=IngestMetadata)
    topics: list[TopicAssignment] = Field(default_factory=list)
    summary_short: str
    summary_long: str

    @field_validator("summary_short", "summary_long")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            raise ValueError("summary must not be empty")
        return cleaned

    @field_validator("summary_short")
    @classmethod
    def _one_sentence(cls, value: str) -> str:
        # "one sentence" is a soft contract; only reject the clearly-wrong.
        if len(value) > 400:
            raise ValueError("summary_short must be one sentence, not a paragraph")
        return value

    @field_validator("topics")
    @classmethod
    def _bounded(cls, value: list[TopicAssignment]) -> list[TopicAssignment]:
        if len(value) > MAX_TOPICS_PER_PAPER:
            raise ValueError(f"at most {MAX_TOPICS_PER_PAPER} topics, got {len(value)}")
        return value
