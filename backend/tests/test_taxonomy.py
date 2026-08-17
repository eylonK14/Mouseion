"""Topic canonicalization and the pick-or-propose contract.

The rule under test is CLAUDE.md's drift control: proposing a name that already
exists — in any casing or spacing — must LINK to the existing topic, never
create a second one.
"""

from __future__ import annotations

import sqlite3

import pytest

from mouseion.services.papers import get_or_create_by_sha256
from mouseion.services.schemas import TopicAssignment
from mouseion.services.taxonomy import (
    apply_topic_assignments,
    find_topic_by_name,
    get_or_create_topic,
    list_topics,
    normalize_topic_name,
    topics_for_paper,
)


@pytest.fixture
def paper_id(conn: sqlite3.Connection) -> int:
    return get_or_create_by_sha256(conn, "a" * 64)[0]


@pytest.fixture
def other_paper_id(conn: sqlite3.Connection) -> int:
    return get_or_create_by_sha256(conn, "b" * 64)[0]


def test_normalize_topic_name_collapses_whitespace() -> None:
    assert normalize_topic_name("  Machine   Learning \n") == "Machine Learning"


def test_get_or_create_topic_is_case_insensitive(conn: sqlite3.Connection) -> None:
    created, was_created = get_or_create_topic(conn, "Machine Learning")
    assert was_created is True

    found, was_created_again = get_or_create_topic(conn, "machine learning")
    assert was_created_again is False
    assert found.id == created.id
    # The original casing is what stays in the library.
    assert found.name == "Machine Learning"
    assert len(list_topics(conn)) == 1


def test_get_or_create_topic_matches_across_whitespace(conn: sqlite3.Connection) -> None:
    first, _ = get_or_create_topic(conn, "Attention Mechanisms")
    second, created = get_or_create_topic(conn, "  attention   mechanisms  ")
    assert created is False
    assert second.id == first.id


def test_proposing_an_existing_name_links_instead_of_duplicating(
    conn: sqlite3.Connection, paper_id: int
) -> None:
    existing, _ = get_or_create_topic(conn, "Machine Learning")

    applied = apply_topic_assignments(
        conn, paper_id, [TopicAssignment(new_topic_name="MACHINE learning", parent=None)]
    )

    assert applied.created == []
    assert [t.id for t in applied.reused] == [existing.id]
    assert applied.topic_ids == [existing.id]
    assert len(list_topics(conn)) == 1


def test_new_topic_is_created_under_the_named_parent(
    conn: sqlite3.Connection, paper_id: int
) -> None:
    parent, _ = get_or_create_topic(conn, "Machine Learning")

    applied = apply_topic_assignments(
        conn, paper_id, [TopicAssignment(new_topic_name="Transformers", parent=parent.id)]
    )

    assert applied.created_names == ["Transformers"]
    child = find_topic_by_name(conn, "Transformers")
    assert child is not None
    assert child.parent_id == parent.id
    assert [t.name for t in topics_for_paper(conn, paper_id)] == ["Transformers"]


def test_second_paper_reuses_the_topic_the_first_created(
    conn: sqlite3.Connection, paper_id: int, other_paper_id: int
) -> None:
    parent, _ = get_or_create_topic(conn, "Machine Learning")

    first = apply_topic_assignments(
        conn, paper_id, [TopicAssignment(new_topic_name="Transformers", parent=parent.id)]
    )
    created_id = first.created[0].id

    # The second paper picks it by id, the way the model is told to.
    second = apply_topic_assignments(
        conn, other_paper_id, [TopicAssignment(existing_topic_id=created_id)]
    )

    assert second.created == []
    assert second.topic_ids == [created_id]
    assert len(list_topics(conn)) == 2  # parent + Transformers, nothing new
    assert [t.name for t in topics_for_paper(conn, other_paper_id)] == ["Transformers"]


def test_unknown_existing_id_is_dropped_not_fatal(
    conn: sqlite3.Connection, paper_id: int
) -> None:
    known, _ = get_or_create_topic(conn, "Machine Learning")

    applied = apply_topic_assignments(
        conn,
        paper_id,
        [
            TopicAssignment(existing_topic_id=9999),
            TopicAssignment(existing_topic_id=known.id),
        ],
    )

    assert applied.topic_ids == [known.id]
    assert any("9999" in message for message in applied.dropped)


def test_proposal_with_unknown_parent_lands_at_root(
    conn: sqlite3.Connection, paper_id: int
) -> None:
    applied = apply_topic_assignments(
        conn, paper_id, [TopicAssignment(new_topic_name="Orphan Topic", parent=4242)]
    )

    topic = find_topic_by_name(conn, "Orphan Topic")
    assert topic is not None
    assert topic.parent_id is None
    assert applied.dropped  # the bad parent is recorded, not silently ignored


def test_duplicate_assignments_link_once(conn: sqlite3.Connection, paper_id: int) -> None:
    topic, _ = get_or_create_topic(conn, "Machine Learning")

    applied = apply_topic_assignments(
        conn,
        paper_id,
        [
            TopicAssignment(existing_topic_id=topic.id),
            TopicAssignment(new_topic_name="machine learning"),
        ],
    )

    assert applied.topic_ids == [topic.id]
    assert len(topics_for_paper(conn, paper_id)) == 1


def test_reapplying_the_same_topics_is_idempotent(
    conn: sqlite3.Connection, paper_id: int
) -> None:
    assignments = [TopicAssignment(new_topic_name="Retrieval")]
    apply_topic_assignments(conn, paper_id, assignments)
    apply_topic_assignments(conn, paper_id, assignments)

    assert len(topics_for_paper(conn, paper_id)) == 1
    assert len(list_topics(conn)) == 1


def test_assignment_requires_exactly_one_of_id_or_name() -> None:
    with pytest.raises(ValueError, match="not both"):
        TopicAssignment(existing_topic_id=1, new_topic_name="Both")
    with pytest.raises(ValueError, match="either existing_topic_id or new_topic_name"):
        TopicAssignment()
