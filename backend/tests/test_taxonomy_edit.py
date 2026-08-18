"""Editing the taxonomy: the counted tree, re-parenting, and merge.

Phase 1's test_taxonomy.py covers canonicalization and ingest-time proposals.
This file covers the manual corrections Phase 2 added.
"""

from __future__ import annotations

import sqlite3

import pytest

from mouseion.services.papers import get_or_create_by_sha256
from mouseion.services.taxonomy import (
    TopicCycleError,
    TopicNameConflictError,
    TopicNotFoundError,
    ancestor_ids,
    build_topic_tree,
    delete_topic,
    descendant_ids,
    get_or_create_topic,
    get_topic,
    merge_topics,
    rename_topic,
    reparent_topic,
    split_topic,
    topics_for_paper,
)


@pytest.fixture
def tree(conn: sqlite3.Connection) -> dict[str, int]:
    """ML → Architectures → Transformers, plus an unrelated Optics root."""
    ml, _ = get_or_create_topic(conn, "Machine Learning")
    arch, _ = get_or_create_topic(conn, "Architectures", ml.id)
    tf, _ = get_or_create_topic(conn, "Transformers", arch.id)
    optics, _ = get_or_create_topic(conn, "Optics")
    return {"ml": ml.id, "arch": arch.id, "tf": tf.id, "optics": optics.id}


def make_paper(conn: sqlite3.Connection, key: str, *topic_ids: int) -> int:
    paper_id, _ = get_or_create_by_sha256(conn, key * 64)
    conn.executemany(
        "INSERT OR IGNORE INTO paper_topics (paper_id, topic_id) VALUES (?, ?)",
        [(paper_id, topic_id) for topic_id in topic_ids],
    )
    return paper_id


def find(nodes, name: str):
    for node in nodes:
        if node.name == name:
            return node
        found = find(node.children, name)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------------- walks
def test_descendant_and_ancestor_walks(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    assert set(descendant_ids(conn, tree["ml"])) == {tree["ml"], tree["arch"], tree["tf"]}
    assert descendant_ids(conn, tree["ml"], include_self=False) == [tree["arch"], tree["tf"]]
    assert descendant_ids(conn, tree["tf"]) == [tree["tf"]]
    assert ancestor_ids(conn, tree["tf"]) == [tree["arch"], tree["ml"]]
    assert ancestor_ids(conn, tree["ml"]) == []


def test_walks_terminate_on_a_cycle(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    """Forced past the API's guard, straight into the table: ML → Architectures
    → Transformers → ML. Both walks must stop rather than loop."""
    conn.execute("UPDATE topics SET parent_id = ? WHERE id = ?", (tree["tf"], tree["ml"]))
    assert set(descendant_ids(conn, tree["ml"])) == {tree["ml"], tree["arch"], tree["tf"]}
    # The walk stops the moment it would revisit its own starting point.
    assert ancestor_ids(conn, tree["tf"]) == [tree["arch"], tree["ml"]]


# -------------------------------------------------------------------- tree
def test_tree_is_nested_and_counts_roll_up(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    make_paper(conn, "a", tree["tf"])
    make_paper(conn, "b", tree["arch"])
    make_paper(conn, "c", tree["optics"])

    nodes = build_topic_tree(conn)
    assert [node.name for node in nodes] == ["Machine Learning", "Optics"]

    ml = find(nodes, "Machine Learning")
    assert ml.paper_count == 0  # nothing is tagged with it directly
    assert ml.total_count == 2  # but two papers sit below it

    arch = find(nodes, "Architectures")
    assert (arch.paper_count, arch.total_count) == (1, 2)
    assert find(nodes, "Transformers").total_count == 1
    assert find(nodes, "Optics").total_count == 1


def test_a_paper_tagged_at_two_levels_is_counted_once(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    make_paper(conn, "a", tree["ml"], tree["tf"])
    ml = find(build_topic_tree(conn), "Machine Learning")
    assert ml.paper_count == 1
    assert ml.total_count == 1


def test_a_topic_whose_parent_vanished_becomes_a_root(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    """A dangling parent_id must not make a topic disappear from the tree.

    The foreign key normally makes this impossible — writing a stale id here
    needs the pragma turned off, which is the point: the tree builder does not
    depend on a connection-level setting for a topic to remain visible.
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("UPDATE topics SET parent_id = 9999 WHERE id = ?", (tree["optics"],))
    finally:
        conn.execute("PRAGMA foreign_keys=ON")

    assert "Optics" in [node.name for node in build_topic_tree(conn)]


# ------------------------------------------------------------------ rename
def test_rename(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    renamed = rename_topic(conn, tree["tf"], "  Transformer   Architectures ")
    assert renamed.name == "Transformer Architectures"
    assert get_topic(conn, tree["tf"]).name == "Transformer Architectures"


def test_rename_can_change_case_but_not_collide(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    assert rename_topic(conn, tree["tf"], "TRANSFORMERS").name == "TRANSFORMERS"
    with pytest.raises(TopicNameConflictError):
        rename_topic(conn, tree["tf"], "optics")  # NOCASE clash with "Optics"


def test_rename_rejects_empty_and_unknown(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        rename_topic(conn, tree["tf"], "   ")
    with pytest.raises(TopicNotFoundError):
        rename_topic(conn, 9999, "Anything")


# ---------------------------------------------------------------- reparent
def test_reparent_moves_a_subtree(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    reparent_topic(conn, tree["arch"], tree["optics"])
    assert get_topic(conn, tree["arch"]).parent_id == tree["optics"]
    # The grandchild travels with its parent.
    assert set(descendant_ids(conn, tree["optics"])) == {
        tree["optics"],
        tree["arch"],
        tree["tf"],
    }


def test_reparent_to_root(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    reparent_topic(conn, tree["tf"], None)
    assert get_topic(conn, tree["tf"]).parent_id is None
    assert "Transformers" in [node.name for node in build_topic_tree(conn)]


def test_reparent_refuses_to_make_a_cycle(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    with pytest.raises(TopicCycleError):
        reparent_topic(conn, tree["ml"], tree["ml"])  # self
    with pytest.raises(TopicCycleError):
        reparent_topic(conn, tree["ml"], tree["arch"])  # direct child
    with pytest.raises(TopicCycleError):
        reparent_topic(conn, tree["ml"], tree["tf"])  # grandchild

    # Nothing moved.
    assert get_topic(conn, tree["ml"]).parent_id is None


def test_reparent_rejects_unknown_ids(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    with pytest.raises(TopicNotFoundError):
        reparent_topic(conn, 9999, tree["ml"])
    with pytest.raises(TopicNotFoundError):
        reparent_topic(conn, tree["ml"], 9999)


# ------------------------------------------------------------------- merge
def test_merge_relinks_papers_and_deletes_the_source(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    only_source = make_paper(conn, "a", tree["tf"])
    already_both = make_paper(conn, "b", tree["tf"], tree["optics"])
    untouched = make_paper(conn, "c", tree["ml"])

    result = merge_topics(conn, tree["tf"], tree["optics"])

    assert result.target.id == tree["optics"]
    assert get_topic(conn, tree["tf"]) is None

    assert [t.id for t in topics_for_paper(conn, only_source)] == [tree["optics"]]
    # No duplicate row for the paper that already had both.
    assert [t.id for t in topics_for_paper(conn, already_both)] == [tree["optics"]]
    assert [t.id for t in topics_for_paper(conn, untouched)] == [tree["ml"]]
    assert result.papers_relinked == 1


def test_merge_moves_the_sources_children_onto_the_target(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    result = merge_topics(conn, tree["arch"], tree["optics"])
    assert result.children_moved == 1
    assert get_topic(conn, tree["tf"]).parent_id == tree["optics"]


def test_merging_a_parent_into_its_own_child_does_not_orphan_the_child(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    """Regression guard: deleting the source sets its children's parent_id to
    NULL, so the target must be spliced into the source's place first — or
    "Architectures" ends up a root instead of sitting under "Machine
    Learning"."""
    merge_topics(conn, tree["arch"], tree["tf"])

    survivor = get_topic(conn, tree["tf"])
    assert survivor is not None
    assert survivor.parent_id == tree["ml"]
    assert get_topic(conn, tree["arch"]) is None
    assert find(build_topic_tree(conn), "Transformers") is not None


def test_merge_counts_are_correct_afterwards(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    make_paper(conn, "a", tree["tf"])
    make_paper(conn, "b", tree["optics"])

    merge_topics(conn, tree["tf"], tree["optics"])

    optics = find(build_topic_tree(conn), "Optics")
    assert (optics.paper_count, optics.total_count) == (2, 2)
    assert find(build_topic_tree(conn), "Transformers") is None


def test_merge_rejects_self_and_unknown(conn: sqlite3.Connection, tree: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        merge_topics(conn, tree["tf"], tree["tf"])
    with pytest.raises(TopicNotFoundError):
        merge_topics(conn, 9999, tree["tf"])
    with pytest.raises(TopicNotFoundError):
        merge_topics(conn, tree["tf"], 9999)


# ------------------------------------------------------------- split/delete
def test_split_creates_peer_and_moves_only_selected_direct_links(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    moved = make_paper(conn, "a", tree["arch"])
    stayed = make_paper(conn, "b", tree["arch"])
    descendant = make_paper(conn, "c", tree["tf"])

    result = split_topic(conn, tree["arch"], "Graph Architectures", [moved])

    assert result.created.parent_id == tree["ml"]
    assert result.papers_moved == 1
    assert [t.id for t in topics_for_paper(conn, moved)] == [result.created.id]
    assert [t.id for t in topics_for_paper(conn, stayed)] == [tree["arch"]]
    assert [t.id for t in topics_for_paper(conn, descendant)] == [tree["tf"]]
    assert get_topic(conn, tree["tf"]).parent_id == tree["arch"]


def test_split_validates_selection_and_name(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    linked = make_paper(conn, "a", tree["arch"])
    unrelated = make_paper(conn, "b", tree["optics"])
    with pytest.raises(ValueError, match="select at least one"):
        split_topic(conn, tree["arch"], "New", [])
    with pytest.raises(ValueError, match="directly assigned"):
        split_topic(conn, tree["arch"], "New", [unrelated])
    with pytest.raises(TopicNameConflictError):
        split_topic(conn, tree["arch"], "Optics", [linked])
    assert get_topic(conn, tree["arch"]) is not None


def test_delete_removes_links_and_promotes_children_without_deleting_papers(
    conn: sqlite3.Connection, tree: dict[str, int]
) -> None:
    paper = make_paper(conn, "a", tree["arch"])
    result = delete_topic(conn, tree["arch"])

    assert result.paper_links_removed == 1
    assert result.children_promoted == 1
    assert get_topic(conn, tree["arch"]) is None
    assert get_topic(conn, tree["tf"]).parent_id == tree["ml"]
    assert conn.execute("SELECT id FROM papers WHERE id = ?", (paper,)).fetchone() is not None
    assert topics_for_paper(conn, paper) == []


def test_delete_rejects_unknown_topic(conn: sqlite3.Connection) -> None:
    with pytest.raises(TopicNotFoundError):
        delete_topic(conn, 9999)
