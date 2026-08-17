"""The topic taxonomy.

CLAUDE.md's drift-control rule lives here: topics are a curated hierarchy, the
model may only pick an existing topic or propose a new one under a named
parent, and names are unique case-insensitively so "Attention Mechanisms" can
never sit beside "attention mechanisms".
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from mouseion.db import transaction
from mouseion.services.schemas import TopicAssignment

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TopicRow:
    id: int
    name: str
    parent_id: int | None


def normalize_topic_name(name: str) -> str:
    """Collapse whitespace and trim. Case is preserved — the *stored* casing is
    whatever created the topic first; matching is case-insensitive."""
    return re.sub(r"\s+", " ", name or "").strip()


def _row(row: sqlite3.Row) -> TopicRow:
    return TopicRow(id=row["id"], name=row["name"], parent_id=row["parent_id"])


def list_topics(conn: sqlite3.Connection) -> list[TopicRow]:
    rows = conn.execute(
        "SELECT id, name, parent_id FROM topics ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return [_row(r) for r in rows]


def get_topic(conn: sqlite3.Connection, topic_id: int) -> TopicRow | None:
    row = conn.execute(
        "SELECT id, name, parent_id FROM topics WHERE id = ?", (topic_id,)
    ).fetchone()
    return _row(row) if row else None


def find_topic_by_name(conn: sqlite3.Connection, name: str) -> TopicRow | None:
    """Case-insensitive lookup (the column is declared COLLATE NOCASE)."""
    row = conn.execute(
        "SELECT id, name, parent_id FROM topics WHERE name = ?", (normalize_topic_name(name),)
    ).fetchone()
    return _row(row) if row else None


def get_or_create_topic(
    conn: sqlite3.Connection, name: str, parent_id: int | None = None
) -> tuple[TopicRow, bool]:
    """Return (topic, created). An existing topic is returned untouched — a new
    proposal never re-parents a topic that is already in the taxonomy."""
    clean = normalize_topic_name(name)
    if not clean:
        raise ValueError("topic name must not be empty")

    existing = find_topic_by_name(conn, clean)
    if existing is not None:
        return existing, False

    try:
        cursor = conn.execute(
            "INSERT INTO topics (name, parent_id) VALUES (?, ?)", (clean, parent_id)
        )
    except sqlite3.IntegrityError:
        # Lost a race with the other process; the winner's row is what we want.
        existing = find_topic_by_name(conn, clean)
        if existing is None:
            raise
        return existing, False

    return TopicRow(id=int(cursor.lastrowid), name=clean, parent_id=parent_id), True


@dataclass(slots=True)
class AppliedTopics:
    topic_ids: list[int] = field(default_factory=list)
    created: list[TopicRow] = field(default_factory=list)
    reused: list[TopicRow] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def created_names(self) -> list[str]:
        return [t.name for t in self.created]


def apply_topic_assignments(
    conn: sqlite3.Connection,
    paper_id: int,
    assignments: Sequence[TopicAssignment],
) -> AppliedTopics:
    """Apply a batch of topic decisions transactionally.

    A single bad decision (an id the model invented, a parent that does not
    exist) is dropped and recorded rather than failing the ingest — the paper
    keeps the topics that were valid.
    """
    result = AppliedTopics()
    with transaction(conn):
        known = {t.id: t for t in list_topics(conn)}
        seen: set[int] = set()

        for assignment in assignments:
            topic: TopicRow | None = None

            if assignment.existing_topic_id is not None:
                topic = known.get(assignment.existing_topic_id)
                if topic is None:
                    result.dropped.append(
                        f"unknown existing_topic_id={assignment.existing_topic_id}"
                    )
                    continue
                result.reused.append(topic)

            else:
                parent_id = assignment.parent
                if parent_id is not None and parent_id not in known:
                    log.warning(
                        "proposal %r named unknown parent %s; creating at root",
                        assignment.new_topic_name,
                        parent_id,
                    )
                    result.dropped.append(
                        f"unknown parent={parent_id} for {assignment.new_topic_name!r}"
                    )
                    parent_id = None

                assert assignment.new_topic_name is not None  # guaranteed by validator
                topic, created = get_or_create_topic(conn, assignment.new_topic_name, parent_id)
                known[topic.id] = topic
                # A proposal whose name already exists (in any casing) is a
                # reuse, not a duplicate — this is the canonicalization rule.
                (result.created if created else result.reused).append(topic)

            if topic.id not in seen:
                seen.add(topic.id)
                result.topic_ids.append(topic.id)

        conn.executemany(
            "INSERT OR IGNORE INTO paper_topics (paper_id, topic_id) VALUES (?, ?)",
            [(paper_id, topic_id) for topic_id in result.topic_ids],
        )

    return result


def topics_for_paper(conn: sqlite3.Connection, paper_id: int) -> list[TopicRow]:
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.parent_id
        FROM topics t
        JOIN paper_topics pt ON pt.topic_id = t.id
        WHERE pt.paper_id = ?
        ORDER BY t.name COLLATE NOCASE
        """,
        (paper_id,),
    ).fetchall()
    return [_row(r) for r in rows]


def topics_for_papers(
    conn: sqlite3.Connection, paper_ids: Iterable[int]
) -> dict[int, list[TopicRow]]:
    """Batch variant — keeps the list view to two queries instead of N+1."""
    ids = list(paper_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""
        SELECT pt.paper_id AS paper_id, t.id, t.name, t.parent_id
        FROM topics t
        JOIN paper_topics pt ON pt.topic_id = t.id
        WHERE pt.paper_id IN ({placeholders})
        ORDER BY t.name COLLATE NOCASE
        """,
        ids,
    ).fetchall()
    out: dict[int, list[TopicRow]] = {paper_id: [] for paper_id in ids}
    for row in rows:
        out[row["paper_id"]].append(_row(row))
    return out
