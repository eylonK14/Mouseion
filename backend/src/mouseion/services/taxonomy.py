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
    """Batch variant — keeps a page of results to two queries instead of N+1."""
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


# ---------------------------------------------------------------------------
# hierarchy walks
#
# The taxonomy is a few hundred rows at most, so every walk below loads the
# whole table once and traverses it in Python. That is both faster than a
# recursive CTE per question and immune to the non-termination a cycle would
# cause in SQL — each walk carries a `seen` set, so it visits a topic at most
# once no matter what the parent pointers say.
# ---------------------------------------------------------------------------
def _children_map(topics: Sequence[TopicRow]) -> dict[int | None, list[TopicRow]]:
    known = {topic.id for topic in topics}
    children: dict[int | None, list[TopicRow]] = {}
    for topic in topics:
        # A parent that no longer exists means the topic is effectively a root.
        parent = topic.parent_id if topic.parent_id in known else None
        children.setdefault(parent, []).append(topic)
    return children


def descendant_ids(
    conn: sqlite3.Connection, topic_id: int, *, include_self: bool = True
) -> list[int]:
    """Every topic at or below `topic_id`, breadth-first."""
    children = _children_map(list_topics(conn))
    out: list[int] = [topic_id] if include_self else []
    seen: set[int] = {topic_id}
    frontier = [topic_id]
    while frontier:
        current = frontier.pop(0)
        for child in children.get(current, []):
            if child.id in seen:
                continue
            seen.add(child.id)
            out.append(child.id)
            frontier.append(child.id)
    return out


def ancestor_ids(conn: sqlite3.Connection, topic_id: int) -> list[int]:
    """Ancestors of `topic_id`, nearest first. Stops on a cycle."""
    by_id = {topic.id: topic for topic in list_topics(conn)}
    out: list[int] = []
    seen: set[int] = {topic_id}
    current = by_id.get(topic_id)
    while current is not None and current.parent_id is not None:
        if current.parent_id in seen:
            break
        seen.add(current.parent_id)
        out.append(current.parent_id)
        current = by_id.get(current.parent_id)
    return out


@dataclass(slots=True)
class TopicNode:
    """One node of the tree returned by `GET /api/topics/tree`.

    `paper_count` is the direct tagging count; `total_count` includes every
    descendant and counts a paper once even when it is tagged with both a
    parent and its child.
    """

    id: int
    name: str
    parent_id: int | None
    paper_count: int = 0
    total_count: int = 0
    children: list[TopicNode] = field(default_factory=list)


def build_topic_tree(conn: sqlite3.Connection) -> list[TopicNode]:
    """The whole taxonomy as a nested, alphabetically ordered forest."""
    topics = list_topics(conn)
    children = _children_map(topics)

    direct: dict[int, set[int]] = {topic.id: set() for topic in topics}
    for row in conn.execute("SELECT topic_id, paper_id FROM paper_topics").fetchall():
        bucket = direct.get(row["topic_id"])
        if bucket is not None:
            bucket.add(row["paper_id"])

    visited: set[int] = set()

    def build(topic: TopicRow) -> tuple[TopicNode, set[int]]:
        visited.add(topic.id)
        mine = direct.get(topic.id, set())
        rolled = set(mine)
        kids: list[TopicNode] = []
        for child in children.get(topic.id, []):
            if child.id in visited:
                continue
            node, below = build(child)
            kids.append(node)
            rolled |= below
        return (
            TopicNode(
                id=topic.id,
                name=topic.name,
                parent_id=topic.parent_id,
                paper_count=len(mine),
                total_count=len(rolled),
                children=kids,
            ),
            rolled,
        )

    return [build(root)[0] for root in children.get(None, [])]


# ---------------------------------------------------------------------------
# editing the taxonomy — the manual escape hatch for drift
# ---------------------------------------------------------------------------
class TopicError(ValueError):
    """Base class for taxonomy edits the caller got wrong."""


class TopicNotFoundError(TopicError):
    pass


class TopicNameConflictError(TopicError):
    pass


class TopicCycleError(TopicError):
    """Re-parenting a topic under itself or one of its own descendants."""


@dataclass(slots=True)
class DeleteResult:
    topic: TopicRow
    paper_links_removed: int
    children_promoted: int


@dataclass(slots=True)
class SplitResult:
    source: TopicRow
    created: TopicRow
    papers_moved: int


def _require(conn: sqlite3.Connection, topic_id: int) -> TopicRow:
    topic = get_topic(conn, topic_id)
    if topic is None:
        raise TopicNotFoundError(f"topic {topic_id} not found")
    return topic


def rename_topic(conn: sqlite3.Connection, topic_id: int, name: str) -> TopicRow:
    """Rename in place. Re-casing a topic is allowed; colliding is not."""
    topic = _require(conn, topic_id)
    clean = normalize_topic_name(name)
    if not clean:
        raise TopicError("topic name must not be empty")

    clash = find_topic_by_name(conn, clean)
    if clash is not None and clash.id != topic_id:
        raise TopicNameConflictError(f"a topic named {clash.name!r} already exists")

    conn.execute("UPDATE topics SET name = ? WHERE id = ?", (clean, topic_id))
    return TopicRow(id=topic.id, name=clean, parent_id=topic.parent_id)


def reparent_topic(conn: sqlite3.Connection, topic_id: int, parent_id: int | None) -> TopicRow:
    """Move a topic under a new parent (None = make it a root). Cycle-safe."""
    topic = _require(conn, topic_id)
    if parent_id is not None:
        if parent_id == topic_id:
            raise TopicCycleError("a topic cannot be its own parent")
        _require(conn, parent_id)
        if parent_id in descendant_ids(conn, topic_id):
            raise TopicCycleError(
                f"topic {parent_id} is below topic {topic_id}; that would make a cycle"
            )

    conn.execute("UPDATE topics SET parent_id = ? WHERE id = ?", (parent_id, topic_id))
    return TopicRow(id=topic.id, name=topic.name, parent_id=parent_id)


@dataclass(slots=True)
class MergeResult:
    target: TopicRow
    papers_relinked: int
    children_moved: int


def merge_topics(conn: sqlite3.Connection, source_id: int, target_id: int) -> MergeResult:
    """Merge topic `source_id` into `target_id`: relink papers, then delete it.

    This is the manual correction for taxonomy drift, so it is deliberately
    total — after it returns, the source id does not exist and nothing that
    referred to it has been lost.
    """
    if source_id == target_id:
        raise TopicError("cannot merge a topic into itself")
    source = _require(conn, source_id)
    _require(conn, target_id)

    with transaction(conn):
        # Merging a parent into one of its own children would orphan the child
        # the moment the parent row disappears (parent_id is ON DELETE SET
        # NULL). Splice the target into the source's place first.
        if target_id in descendant_ids(conn, source_id, include_self=False):
            conn.execute(
                "UPDATE topics SET parent_id = ? WHERE id = ?", (source.parent_id, target_id)
            )

        relinked = conn.execute(
            """
            INSERT OR IGNORE INTO paper_topics (paper_id, topic_id)
            SELECT paper_id, ? FROM paper_topics WHERE topic_id = ?
            """,
            (target_id, source_id),
        ).rowcount

        moved = conn.execute(
            "UPDATE topics SET parent_id = ? WHERE parent_id = ?", (target_id, source_id)
        ).rowcount

        # paper_topics rows for the source go with it (ON DELETE CASCADE).
        conn.execute("DELETE FROM topics WHERE id = ?", (source_id,))

    target = get_topic(conn, target_id)
    assert target is not None
    return MergeResult(
        target=target, papers_relinked=max(relinked, 0), children_moved=max(moved, 0)
    )


def delete_topic(conn: sqlite3.Connection, topic_id: int) -> DeleteResult:
    """Delete one topic without deleting its subtree or any papers.

    Direct paper links disappear with the topic. Children are promoted to the
    deleted topic's parent so the rest of the curated hierarchy stays intact.
    """
    topic = _require(conn, topic_id)
    with transaction(conn):
        links = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM paper_topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()["n"]
        )
        promoted = conn.execute(
            "UPDATE topics SET parent_id = ? WHERE parent_id = ?", (topic.parent_id, topic_id)
        ).rowcount
        conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    return DeleteResult(topic, links, max(promoted, 0))


def split_topic(
    conn: sqlite3.Connection, topic_id: int, new_name: str, paper_ids: Sequence[int]
) -> SplitResult:
    """Create a peer topic and move selected direct paper links into it."""
    source = _require(conn, topic_id)
    selected = list(dict.fromkeys(paper_ids))
    if not selected:
        raise TopicError("select at least one paper to move")

    placeholders = ", ".join("?" for _ in selected)
    linked = {
        int(row["paper_id"])
        for row in conn.execute(
            f"SELECT paper_id FROM paper_topics WHERE topic_id = ? AND paper_id IN ({placeholders})",
            (topic_id, *selected),
        )
    }
    if linked != set(selected):
        raise TopicError("every selected paper must be directly assigned to the source topic")

    clean = normalize_topic_name(new_name)
    if not clean:
        raise TopicError("new topic name must not be empty")
    if find_topic_by_name(conn, clean) is not None:
        raise TopicNameConflictError(f"a topic named {clean!r} already exists")

    with transaction(conn):
        created, was_created = get_or_create_topic(conn, clean, source.parent_id)
        if not was_created:
            raise TopicNameConflictError(f"a topic named {clean!r} already exists")
        conn.executemany(
            "INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)",
            [(paper_id, created.id) for paper_id in selected],
        )
        conn.executemany(
            "DELETE FROM paper_topics WHERE paper_id = ? AND topic_id = ?",
            [(paper_id, topic_id) for paper_id in selected],
        )
    return SplitResult(source, created, len(selected))


def papers_by_topic(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """All direct topic assignments for the taxonomy split controls, batched."""
    rows = conn.execute(
        """
        SELECT pt.topic_id, p.id, p.title, p.authors, p.year
        FROM paper_topics pt
        JOIN papers p ON p.id = pt.paper_id
        ORDER BY pt.topic_id, COALESCE(p.title, ''), p.id
        """
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["topic_id"]), []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# paper ↔ topic links
# ---------------------------------------------------------------------------
def add_paper_topic(conn: sqlite3.Connection, paper_id: int, topic_id: int) -> TopicRow:
    """Tag a paper with an *existing* topic.

    There is no create-on-the-fly variant on purpose: CLAUDE.md's drift rule
    says topics are picked from the taxonomy, never typed free-form. Creating a
    topic is its own deliberate act.
    """
    topic = _require(conn, topic_id)
    conn.execute(
        "INSERT OR IGNORE INTO paper_topics (paper_id, topic_id) VALUES (?, ?)",
        (paper_id, topic_id),
    )
    return topic


def remove_paper_topic(conn: sqlite3.Connection, paper_id: int, topic_id: int) -> bool:
    cursor = conn.execute(
        "DELETE FROM paper_topics WHERE paper_id = ? AND topic_id = ?", (paper_id, topic_id)
    )
    return cursor.rowcount > 0
