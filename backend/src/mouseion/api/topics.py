"""Taxonomy endpoints — browsing the tree and correcting drift by hand.

CLAUDE.md keeps topics as curated metadata, so the interesting operations are
not "create a tag" but "this topic should have been that one" (merge) and
"this belongs under that" (re-parent).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status

from mouseion.api.models import (
    TopicCreateIn,
    TopicDeleteOut,
    TopicMergeIn,
    TopicMergeOut,
    TopicNodeOut,
    TopicOut,
    TopicPatchIn,
    TopicSplitIn,
    TopicSplitOut,
    TopicTreeOut,
)
from mouseion.db import get_db
from mouseion.services.taxonomy import (
    TopicCycleError,
    TopicError,
    TopicNameConflictError,
    TopicNotFoundError,
    build_topic_tree,
    delete_topic,
    get_or_create_topic,
    get_topic,
    list_topics,
    merge_topics,
    rename_topic,
    reparent_topic,
    split_topic,
)

router = APIRouter(prefix="/api", tags=["topics"])

# Spelled as a literal: Starlette renamed HTTP_422_UNPROCESSABLE_ENTITY to
# ..._CONTENT, and the old name warns on newer releases while the new one is
# missing on older ones. The number is stable.
_UNPROCESSABLE = 422


def _http(exc: TopicError) -> HTTPException:
    """Map taxonomy errors onto the status codes the UI branches on."""
    if isinstance(exc, TopicNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, TopicNameConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, TopicCycleError):
        return HTTPException(_UNPROCESSABLE, str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/topics", response_model=list[TopicOut])
def list_all_topics(conn: sqlite3.Connection = Depends(get_db)) -> list[TopicOut]:
    """Flat list — what a topic picker needs."""
    return [TopicOut.from_row(topic) for topic in list_topics(conn)]


@router.get("/topics/tree", response_model=TopicTreeOut)
def topic_tree(conn: sqlite3.Connection = Depends(get_db)) -> TopicTreeOut:
    """Nested taxonomy with per-topic counts.

    `total_count` includes descendants and de-duplicates: a paper tagged with
    both "Transformers" and its parent "Architectures" counts once under
    "Architectures".
    """
    nodes = build_topic_tree(conn)
    return TopicTreeOut(
        items=[TopicNodeOut.from_node(node) for node in nodes],
        total_topics=len(list_topics(conn)),
    )


@router.post("/topics", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
def create_topic(
    body: TopicCreateIn, conn: sqlite3.Connection = Depends(get_db)
) -> TopicOut:
    """Add a topic by hand.

    Not in the Phase 2 build list, but the taxonomy page cannot reorganise a
    hierarchy it has no way to add a node to — and the alternative is waiting
    for an ingest to happen to propose the right one.
    """
    if body.parent_id is not None and get_topic(conn, body.parent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"topic {body.parent_id} not found")
    try:
        topic, _ = get_or_create_topic(conn, body.name, body.parent_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return TopicOut.from_row(topic)


@router.patch("/topics/{topic_id}", response_model=TopicOut)
def patch_topic(
    topic_id: int, body: TopicPatchIn, conn: sqlite3.Connection = Depends(get_db)
) -> TopicOut:
    """Rename and/or re-parent. Re-parenting is cycle-safe.

    `parent_id` is tri-state — omit it to leave the parent alone, send `null`
    to promote the topic to a root.
    """
    topic = get_topic(conn, topic_id)
    if topic is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"topic {topic_id} not found")

    try:
        if body.name is not None:
            topic = rename_topic(conn, topic_id, body.name)
        if "parent_id" in body.model_fields_set:
            topic = reparent_topic(conn, topic_id, body.parent_id)
    except TopicError as exc:
        raise _http(exc) from exc

    return TopicOut.from_row(topic)


@router.post("/topics/{topic_id}/merge", response_model=TopicMergeOut)
def merge_topic(
    topic_id: int, body: TopicMergeIn, conn: sqlite3.Connection = Depends(get_db)
) -> TopicMergeOut:
    """Merge `topic_id` into `into_topic_id`, then delete `topic_id`.

    Papers are relinked (duplicates collapse), children are re-parented onto
    the target, and no paper loses a topic in the process.
    """
    try:
        result = merge_topics(conn, topic_id, body.into_topic_id)
    except TopicError as exc:
        raise _http(exc) from exc

    return TopicMergeOut(
        target=TopicOut.from_row(result.target),
        papers_relinked=result.papers_relinked,
        children_moved=result.children_moved,
    )


@router.post("/topics/{topic_id}/split", response_model=TopicSplitOut)
def split_existing_topic(
    topic_id: int, body: TopicSplitIn, conn: sqlite3.Connection = Depends(get_db)
) -> TopicSplitOut:
    try:
        result = split_topic(conn, topic_id, body.name, body.paper_ids)
    except TopicError as exc:
        raise _http(exc) from exc
    return TopicSplitOut(
        source=TopicOut.from_row(result.source),
        created=TopicOut.from_row(result.created),
        papers_moved=result.papers_moved,
    )


@router.delete("/topics/{topic_id}", response_model=TopicDeleteOut)
def delete_existing_topic(
    topic_id: int, conn: sqlite3.Connection = Depends(get_db)
) -> TopicDeleteOut:
    try:
        result = delete_topic(conn, topic_id)
    except TopicError as exc:
        raise _http(exc) from exc
    return TopicDeleteOut(
        deleted=TopicOut.from_row(result.topic),
        paper_links_removed=result.paper_links_removed,
        children_promoted=result.children_promoted,
    )
