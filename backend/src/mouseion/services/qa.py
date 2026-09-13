"""Grounded QA retrieval, context assembly, citation checks, and cost logging.

Collection QA is deliberately two-stage: paper-level vec0 + FTS reciprocal-rank
fusion, followed by a bounded MODEL_QA navigation of each selected paper's
stored tree.  The synthesis call is made by the API stream adapter, but every
piece of reusable library/domain behavior lives here.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from time import perf_counter
from typing import Literal, Sequence

from mouseion.config import Settings, get_settings
from mouseion.db import load_sqlite_vec, serialize_f32
from mouseion.observability import current_request_id
from mouseion.services.embeddings import get_embedder
from mouseion.services.llm import LLMClient, LLMMetrics
from mouseion.services.pdfs import extract_text
from mouseion.services.prompts import GroundingExcerpt, QAPrompt, build_grounding_prompt
from mouseion.services.search import SearchQuery, search_papers
from mouseion.services.taxonomy import (
    TopicRow,
    descendant_ids,
    find_topic_by_name,
    get_topic,
)
from mouseion.services.tree_indexer import TreeNode, load_tree, navigate

RRF_RANK_CONSTANT = 60
_CITATION_RE = re.compile(r"\[([^\]\n]+?)\s+§\s*([^\]\n]+?)\]")


class QAError(RuntimeError):
    """A readable error that can cross either SSE transport."""


class QANotFoundError(QAError):
    pass


@dataclass(frozen=True, slots=True)
class HybridCandidate:
    paper_id: int
    score: float
    vector_rank: int | None = None
    fts_rank: int | None = None
    vector_distance: float | None = None


@dataclass(frozen=True, slots=True)
class SectionGrounding:
    section: str
    text: str
    start_page: int | None = None
    end_page: int | None = None


@dataclass(frozen=True, slots=True)
class PaperGrounding:
    paper_id: int
    title: str
    score: float
    summary_short: str | None
    summary_long: str | None
    sections: tuple[SectionGrounding, ...]

    @property
    def context_chars(self) -> int:
        return sum(
            len(section.text) + len(section.section) + len(self.title) + 48
            for section in self.sections
        )


@dataclass(slots=True)
class PreparedQA:
    scope: Literal["collection", "paper"]
    question: str
    prior_messages: list[dict[str, str]]
    materials: list[PaperGrounding]
    prompt: QAPrompt
    metrics: LLMMetrics = field(default_factory=LLMMetrics)
    paper_id: int | None = None
    topic: TopicRow | None = None
    candidate_count: int = 0
    navigation_count: int = 0

    def metadata(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "paper_id": self.paper_id,
            "topic": (
                {"id": self.topic.id, "name": self.topic.name}
                if self.topic is not None
                else None
            ),
            "consulted_papers": [
                {
                    "id": material.paper_id,
                    "title": material.title,
                    "score": material.score,
                    "sections": [section.section for section in material.sections],
                }
                for material in self.materials
            ],
        }


@dataclass(frozen=True, slots=True)
class PaperMatch:
    id: int
    title: str
    score: float


@dataclass(frozen=True, slots=True)
class PaperResolution:
    locked: PaperMatch | None
    matches: tuple[PaperMatch, ...]


def reciprocal_rank_fusion(
    vector_ids: Sequence[int], fts_ids: Sequence[int], *, limit: int
) -> list[HybridCandidate]:
    """Merge two ranked id lists with deterministic reciprocal-rank fusion."""
    scores: dict[int, float] = {}
    vector_ranks: dict[int, int] = {}
    fts_ranks: dict[int, int] = {}
    for ranks, target in ((vector_ids, vector_ranks), (fts_ids, fts_ranks)):
        seen: set[int] = set()
        for rank, paper_id in enumerate(ranks, start=1):
            if paper_id in seen:
                continue
            seen.add(paper_id)
            target[paper_id] = rank
            scores[paper_id] = scores.get(paper_id, 0.0) + 1.0 / (
                RRF_RANK_CONSTANT + rank
            )
    ordered = sorted(
        scores,
        key=lambda paper_id: (
            -scores[paper_id],
            min(vector_ranks.get(paper_id, 10**9), fts_ranks.get(paper_id, 10**9)),
            paper_id,
        ),
    )
    return [
        HybridCandidate(
            paper_id=paper_id,
            score=scores[paper_id],
            vector_rank=vector_ranks.get(paper_id),
            fts_rank=fts_ranks.get(paper_id),
        )
        for paper_id in ordered[: max(0, limit)]
    ]


def resolve_topic_scope(conn: sqlite3.Connection, scope: int | str | None) -> TopicRow | None:
    if scope is None:
        return None
    if isinstance(scope, int) or (isinstance(scope, str) and scope.strip().isdigit()):
        topic = get_topic(conn, int(scope))
    else:
        topic = find_topic_by_name(conn, str(scope))
    if topic is None:
        raise QANotFoundError(f"topic {scope!r} is not in the library taxonomy")
    return topic


def _vector_ranking(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int,
    topic: TopicRow | None,
) -> tuple[list[int], dict[int, float]]:
    if limit <= 0 or not load_sqlite_vec(conn):
        return [], {}
    total = int(conn.execute("SELECT COUNT(*) AS n FROM paper_vectors").fetchone()["n"])
    if total == 0:
        return [], {}

    vector = get_embedder().encode(question)
    blob = serialize_f32(vector)
    # vec0 has no relational subquery filter in migration 0001. For a topic
    # scope, ask vec0 for the complete distance ordering, then apply the small
    # controlled subtree in Python. This is still the vec0 KNN path and yields
    # the true top-k *inside* the subtree instead of filtering a global top-k.
    search_k = total if topic is not None else min(limit, total)
    rows = conn.execute(
        """
        SELECT paper_id, distance
        FROM paper_vectors
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (blob, search_k),
    ).fetchall()
    allowed: set[int] | None = None
    if topic is not None:
        topic_ids = descendant_ids(conn, topic.id)
        placeholders = ",".join("?" for _ in topic_ids)
        allowed = {
            int(row["paper_id"])
            for row in conn.execute(
                f"SELECT DISTINCT paper_id FROM paper_topics WHERE topic_id IN ({placeholders})",
                topic_ids,
            ).fetchall()
        }
    filtered = [row for row in rows if allowed is None or int(row["paper_id"]) in allowed]
    selected = filtered[:limit]
    return (
        [int(row["paper_id"]) for row in selected],
        {int(row["paper_id"]): float(row["distance"]) for row in selected},
    )


def hybrid_candidates(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int = 8,
    topic_scope: int | str | None = None,
) -> tuple[list[HybridCandidate], TopicRow | None]:
    """Stage 1: local vec0 shortlist merged with exact-term FTS via RRF."""
    topic = resolve_topic_scope(conn, topic_scope)
    vector_ids, distances = _vector_ranking(
        conn, question, limit=limit, topic=topic
    )
    fts_query = SearchQuery(
        q=question,
        topic_id=topic.id if topic else None,
        sort="relevance",
        limit=limit,
    )
    # A punctuation-only question has no FTS terms. `search_papers` normally
    # interprets that as browse, which is correct for the library UI but would
    # turn recent papers into false QA candidates.
    fts_ids = (
        [int(row["id"]) for row in search_papers(conn, fts_query).rows]
        if fts_query.match is not None
        else []
    )
    merged = reciprocal_rank_fusion(vector_ids, fts_ids, limit=limit)
    return ([replace(item, vector_distance=distances.get(item.paper_id)) for item in merged], topic)


def _paper_row(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT p.*, tx.full_text, tx.n_pages
        FROM papers p
        LEFT JOIN paper_texts tx ON tx.paper_id = p.id
        WHERE p.id = ?
        """,
        (paper_id,),
    ).fetchone()
    if row is None:
        raise QANotFoundError(f"paper {paper_id} was not found")
    return row


def _title(row: sqlite3.Row) -> str:
    return (row["title"] or "").strip() or f"Untitled paper {row['id']}"


def _pages_for_nodes(
    page_texts: Sequence[str], nodes: Sequence[TreeNode], *, per_paper_budget: int
) -> list[SectionGrounding]:
    if not page_texts or not nodes:
        return []
    per_section = max(500, per_paper_budget // len(nodes))
    excerpts: list[SectionGrounding] = []
    for node in nodes:
        start = max(1, node.start_page or 1)
        end = min(len(page_texts), node.end_page or start)
        if end < start:
            end = start
        text = "\n\n".join(page_texts[start - 1 : end]).strip()[:per_section].rstrip()
        if text:
            excerpts.append(
                SectionGrounding(node.title or f"Pages {start}–{end}", text, start, end)
            )
    return excerpts


def _text_for_nodes(
    full_text: str, nodes: Sequence[TreeNode], *, per_paper_budget: int
) -> list[SectionGrounding]:
    if not full_text.strip() or not nodes:
        return []
    per_section = max(500, per_paper_budget // len(nodes))
    lowered = full_text.casefold()
    excerpts: list[SectionGrounding] = []
    for node in nodes:
        position = lowered.find(node.title.casefold()) if node.title else -1
        start = position if position >= 0 else 0
        text = full_text[start : start + per_section].strip()
        if text:
            excerpts.append(
                SectionGrounding(
                    node.title or "Relevant section", text, node.start_page, node.end_page
                )
            )
    return excerpts


async def gather_grounding_material_for_paper(
    conn: sqlite3.Connection,
    paper_id: int,
    question: str,
    *,
    client: LLMClient,
    score: float = 1.0,
    metrics: LLMMetrics | None = None,
    settings: Settings | None = None,
    navigate_tree: bool = True,
) -> PaperGrounding:
    """Reusable Phase 4 seam: summaries plus question-relevant paper sections."""
    settings = settings or get_settings()
    row = _paper_row(conn, paper_id)
    tree = load_tree(paper_id, conn, settings)
    nodes: list[TreeNode] = []
    if navigate_tree and tree is not None:
        nodes = await navigate(
            tree,
            question,
            settings.qa_sections_per_paper,
            client=client,
            metrics=metrics,
            text_budget=settings.qa_tree_budget_chars,
        )

    full_text = str(row["full_text"] or "")
    excerpts: list[SectionGrounding] = []
    pdf_path = settings.pdf_dir / f"{row['sha256']}.pdf"
    if nodes and pdf_path.exists():
        extracted = await asyncio.to_thread(extract_text, Path(pdf_path))
        excerpts = _pages_for_nodes(
            extracted.page_texts, nodes, per_paper_budget=settings.qa_section_budget_chars
        )
    if nodes and not excerpts:
        excerpts = _text_for_nodes(
            full_text, nodes, per_paper_budget=settings.qa_section_budget_chars
        )

    if not excerpts and full_text.strip():
        excerpts.append(
            SectionGrounding(
                "Whole paper",
                full_text.strip()[: settings.qa_section_budget_chars].rstrip(),
                1 if row["n_pages"] else None,
                int(row["n_pages"]) if row["n_pages"] else None,
            )
        )
    if row["summary_long"]:
        # Summaries are stored library evidence and are especially valuable for
        # single-paper contribution questions and Phase 4 examiner rubrics.
        excerpts.insert(0, SectionGrounding("Library summary", str(row["summary_long"])))
    if not excerpts and row["abstract"]:
        excerpts.append(SectionGrounding("Abstract", str(row["abstract"])))

    return PaperGrounding(
        paper_id=paper_id,
        title=_title(row),
        score=score,
        summary_short=row["summary_short"],
        summary_long=row["summary_long"],
        sections=tuple(excerpts),
    )


def budget_grounding_material(
    materials: Sequence[PaperGrounding], *, budget_chars: int
) -> list[PaperGrounding]:
    """Fit context by evicting weakest papers before trimming the strongest."""
    kept = list(materials)
    while len(kept) > 1 and sum(item.context_chars for item in kept) > budget_chars:
        weakest = min(range(len(kept)), key=lambda index: (kept[index].score, -index))
        kept.pop(weakest)
    if not kept or sum(item.context_chars for item in kept) <= budget_chars:
        return kept

    # One paper remains and still exceeds the hard cap. Preserve section order
    # and trim only its tail, ensuring the prompt can never grow without bound.
    material = kept[0]
    remaining = max(0, budget_chars)
    sections: list[SectionGrounding] = []
    for section in material.sections:
        overhead = len(material.title) + len(section.section) + 48
        available = remaining - overhead
        if available <= 0:
            break
        text = section.text[:available].rstrip()
        if text:
            sections.append(replace(section, text=text))
            remaining -= overhead + len(text)
    return [replace(material, sections=tuple(sections))] if sections else []


def _prompt_excerpts(materials: Sequence[PaperGrounding]) -> list[GroundingExcerpt]:
    return [
        GroundingExcerpt(
            paper_id=material.paper_id,
            paper_title=material.title,
            section=section.section,
            text=section.text,
        )
        for material in materials
        for section in material.sections
    ]


def _bound_history(
    messages: Sequence[dict[str, str]], *, max_messages: int, budget_chars: int
) -> list[dict[str, str]]:
    """Keep the newest conversational turns inside a separate prompt budget."""
    if max_messages <= 0 or budget_chars <= 0:
        return []
    kept_reversed: list[dict[str, str]] = []
    remaining = budget_chars
    for message in reversed(list(messages)[-max_messages:]):
        content = message.get("content", "").strip()
        if not content or remaining <= 0:
            continue
        bounded = content[-remaining:]
        kept_reversed.append({"role": message.get("role", ""), "content": bounded})
        remaining -= len(bounded)
    return list(reversed(kept_reversed))


async def prepare_collection_qa(
    conn: sqlite3.Connection,
    question: str,
    prior_messages: Sequence[dict[str, str]],
    *,
    client: LLMClient,
    topic_scope: int | str | None = None,
    settings: Settings | None = None,
) -> PreparedQA:
    settings = settings or get_settings()
    metrics = LLMMetrics(model=settings.model_qa)
    candidates, topic = hybrid_candidates(
        conn, question, limit=settings.qa_candidate_k, topic_scope=topic_scope
    )
    navigated = candidates[: settings.qa_max_navigations]
    materials: list[PaperGrounding] = []
    navigation_count = 0
    for candidate in navigated:
        tree = load_tree(candidate.paper_id, conn, settings)
        has_nodes = tree is not None and next(tree.iter_nodes(), None) is not None
        materials.append(
            await gather_grounding_material_for_paper(
                conn,
                candidate.paper_id,
                question,
                client=client,
                score=candidate.score,
                metrics=metrics,
                settings=settings,
            )
        )
        navigation_count += int(has_nodes)
    materials = budget_grounding_material(
        materials, budget_chars=settings.qa_context_budget_chars
    )
    return PreparedQA(
        scope="collection",
        question=question,
        prior_messages=_bound_history(
            prior_messages,
            max_messages=settings.qa_history_messages,
            budget_chars=settings.qa_history_budget_chars,
        ),
        materials=materials,
        prompt=build_grounding_prompt(question=question, excerpts=_prompt_excerpts(materials)),
        metrics=metrics,
        topic=topic,
        candidate_count=len(candidates),
        navigation_count=navigation_count,
    )


async def prepare_paper_qa(
    conn: sqlite3.Connection,
    paper_id: int,
    question: str,
    prior_messages: Sequence[dict[str, str]],
    *,
    client: LLMClient,
    settings: Settings | None = None,
) -> PreparedQA:
    settings = settings or get_settings()
    metrics = LLMMetrics(model=settings.model_qa)
    material = await gather_grounding_material_for_paper(
        conn,
        paper_id,
        question,
        client=client,
        metrics=metrics,
        settings=settings,
    )
    materials = budget_grounding_material(
        [material], budget_chars=settings.qa_context_budget_chars
    )
    return PreparedQA(
        scope="paper",
        question=question,
        prior_messages=_bound_history(
            prior_messages,
            max_messages=settings.qa_history_messages,
            budget_chars=settings.qa_history_budget_chars,
        ),
        materials=materials,
        prompt=build_grounding_prompt(question=question, excerpts=_prompt_excerpts(materials)),
        metrics=metrics,
        paper_id=paper_id,
        candidate_count=1,
        navigation_count=int(
            (tree := load_tree(paper_id, conn, settings)) is not None
            and next(tree.iter_nodes(), None) is not None
        ),
    )


def synthesis_messages(prepared: PreparedQA) -> list[dict[str, str]]:
    history = [
        message
        for message in prepared.prior_messages
        if message.get("role") in {"user", "assistant"} and message.get("content", "").strip()
    ]
    return [
        {"role": "system", "content": prepared.prompt.system},
        *history,
        {"role": "user", "content": prepared.prompt.user},
    ]


def citation_warnings(answer: str, materials: Sequence[PaperGrounding]) -> list[str]:
    """Return every citation whose title was not actually placed in context."""
    available = {material.title.casefold() for material in materials}
    warnings: list[str] = []
    for title, section in _CITATION_RE.findall(answer):
        if title.strip().casefold() not in available:
            label = f"[{title.strip()} §{section.strip()}]"
            if label not in warnings:
                warnings.append(label)
    return warnings


def resolve_paper_from_text(
    conn: sqlite3.Connection, text: str, *, limit: int = 5
) -> PaperResolution:
    """Fuzzy title lock used by the thin single-paper Open WebUI pipe."""
    query = " ".join(text.casefold().split())
    query_tokens = set(re.findall(r"[\w]+", query))
    matches: list[PaperMatch] = []
    for row in conn.execute(
        "SELECT id, title FROM papers WHERE title IS NOT NULL AND trim(title) <> ''"
    ).fetchall():
        title = str(row["title"]).strip()
        normalized = " ".join(title.casefold().split())
        title_tokens = set(re.findall(r"[\w]+", normalized))
        containment = (
            len(title_tokens & query_tokens) / len(title_tokens) if title_tokens else 0.0
        )
        sequence = SequenceMatcher(None, normalized, query).ratio()
        score = max(containment, sequence)
        matches.append(PaperMatch(int(row["id"]), title, score))
    matches.sort(key=lambda match: (-match.score, match.title.casefold(), match.id))
    top = tuple(matches[:limit])
    locked: PaperMatch | None = None
    if top and top[0].score >= 0.58:
        if len(top) == 1 or top[0].score - top[1].score >= 0.14 or top[0].score >= 0.95:
            locked = top[0]
    return PaperResolution(locked=locked, matches=top)


def record_qa_log(
    conn: sqlite3.Connection,
    prepared: PreparedQA,
    *,
    started_at: float,
    citation_issues: Sequence[str] = (),
    error: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO qa_log (
            scope, paper_id, topic_id, model, prompt_tokens, completion_tokens,
            total_tokens, latency_ms, candidate_count, navigation_count,
            consulted_papers_json, citation_warnings_json, error, request_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prepared.scope,
            prepared.paper_id,
            prepared.topic.id if prepared.topic else None,
            prepared.metrics.model or get_settings().model_qa,
            prepared.metrics.prompt_tokens,
            prepared.metrics.completion_tokens,
            prepared.metrics.total_tokens,
            max(0, round((perf_counter() - started_at) * 1000)),
            prepared.candidate_count,
            prepared.navigation_count,
            json.dumps(prepared.metadata()["consulted_papers"], ensure_ascii=False),
            json.dumps(list(citation_issues), ensure_ascii=False),
            error,
            current_request_id(),
        ),
    )
    return int(cursor.lastrowid)
