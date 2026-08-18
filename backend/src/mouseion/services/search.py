"""Search over the FTS5 index built by migration 0001.

Three things live here and nowhere else:

* `build_fts_query` — user text → a safe FTS5 MATCH expression. Every term is
  quoted, so a user typing `AND`, `NEAR`, `-`, `*` or an unbalanced paren gets
  a literal search instead of a syntax error.
* `search_papers` — one parametrised query that covers both "search" and
  "filtered browse". Empty `q` simply drops the MATCH clause and the FTS join.
* `highlight` — turns the sentinel-marked snippet FTS returns into HTML that is
  safe to inject. See the comment on `_MARK_OPEN`.

The response shape (`SearchHit.paper` + `score` + `snippet`) is deliberately
stable: Phase 3 reuses it to render "papers consulted" for a QA answer with the
same card component.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from html import escape
from typing import Any, Literal

SortKey = Literal["relevance", "added_at", "year"]
SORT_KEYS: tuple[SortKey, ...] = ("relevance", "added_at", "year")

# Column order in papers_fts, and how much each is worth. Title matches beat
# body matches by an order of magnitude; full text is the tiebreaker of last
# resort because it is the only field where a passing mention is common.
BM25_WEIGHTS = (10.0, 6.0, 4.0, 4.0, 2.0, 1.0)

# snippet() writes these around each match. They are C0 control characters, so
# they cannot occur in extracted PDF text or in anything a model produced —
# which is what lets `highlight` escape the snippet *first* and then swap the
# sentinels for <mark>, instead of trusting FTS output as HTML.
_MARK_OPEN = "\x02"
_MARK_CLOSE = "\x03"
_SNIPPET_TOKENS = 20

# Depth ceiling on the descendant walk. The re-parent endpoint prevents cycles,
# but a recursive CTE over a cycle does not terminate, so the query refuses to
# trust that invariant.
MAX_TOPIC_DEPTH = 32

_TERM_RE = re.compile(r'"([^"]*)"|(\S+)')


def _clean_term(term: str) -> str:
    """Strip quotes and reject terms FTS5 would tokenize to nothing.

    Quotes are replaced rather than doubled: it sidesteps the escaping rules
    entirely, and a stray quote inside a word is never meaningful anyway.
    """
    cleaned = term.replace('"', " ").strip()
    return cleaned if any(ch.isalnum() for ch in cleaned) else ""


def build_fts_query(raw: str | None) -> str | None:
    """Build an FTS5 MATCH expression, or None when there is nothing to match.

    `"exact phrase"` survives as a phrase; everything else is AND-ed. The final
    bare term gets a `*` so search-as-you-type matches the word being typed
    (`quantum comp` finds "quantum computing").
    """
    text = (raw or "").strip()
    if not text:
        return None

    terms: list[tuple[str, bool]] = []  # (term, was_quoted)
    for quoted, bare in _TERM_RE.findall(text):
        cleaned = _clean_term(quoted if quoted else bare)
        if cleaned:
            terms.append((cleaned, bool(quoted)))

    if not terms:
        return None

    parts = [f'"{term}"' for term, _ in terms]
    if not terms[-1][1]:
        # A quoted phrase is an explicit request for that exact wording, so it
        # is never silently widened into a prefix search.
        parts[-1] += "*"
    return " AND ".join(parts)


def highlight(snippet: str | None) -> str | None:
    """Sentinel-marked snippet → HTML-escaped text with <mark> highlights.

    The escape happens before the sentinels are replaced, so the surrounding
    text — arbitrary PDF content — can never inject markup.
    """
    if snippet is None:
        return None
    return (
        escape(snippet, quote=False)
        .replace(_MARK_OPEN, "<mark>")
        .replace(_MARK_CLOSE, "</mark>")
    )


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """The request, echoed back on the response so a client can render state."""

    q: str | None = None
    topic_id: int | None = None
    status: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    sort: SortKey = "added_at"
    limit: int = 25
    offset: int = 0

    @property
    def match(self) -> str | None:
        return build_fts_query(self.q)


@dataclass(slots=True)
class SearchResult:
    rows: list[sqlite3.Row]
    total: int
    query: SearchQuery


def resolve_sort(sort: str | None, *, has_query: bool) -> SortKey:
    """Default to relevance when searching, recency when browsing.

    Relevance is meaningless without a MATCH, so asking for it while browsing
    quietly falls back rather than erroring — the UI keeps one sort control
    across both modes.
    """
    if sort in SORT_KEYS:
        chosen: SortKey = sort  # type: ignore[assignment]
    else:
        chosen = "relevance" if has_query else "added_at"
    if chosen == "relevance" and not has_query:
        return "added_at"
    return chosen


_TOPIC_SCOPE_CTE = f"""
WITH RECURSIVE topic_scope(id, depth) AS (
    SELECT :topic_id, 0
    UNION ALL
    SELECT t.id, ts.depth + 1
    FROM topics t
    JOIN topic_scope ts ON t.parent_id = ts.id
    WHERE ts.depth < {MAX_TOPIC_DEPTH}
)
"""

_TOPIC_FILTER = """
p.id IN (
    SELECT pt.paper_id FROM paper_topics pt
    WHERE pt.topic_id IN (SELECT id FROM topic_scope)
)
"""


def _where(query: SearchQuery, match: str | None) -> tuple[list[str], dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if match is not None:
        clauses.append("papers_fts MATCH :match")
        params["match"] = match
    if query.topic_id is not None:
        clauses.append(_TOPIC_FILTER)
        params["topic_id"] = query.topic_id
    if query.status:
        clauses.append("p.status = :status")
        params["status"] = query.status
    if query.year_from is not None:
        clauses.append("p.year IS NOT NULL AND p.year >= :year_from")
        params["year_from"] = query.year_from
    if query.year_to is not None:
        clauses.append("p.year IS NOT NULL AND p.year <= :year_to")
        params["year_to"] = query.year_to

    return clauses, params


def _from_clause(match: str | None) -> str:
    """With a MATCH, papers_fts drives the query; without one it is not joined.

    bm25() and snippet() are only legal against a table that is being matched,
    which is why the browse path is a genuinely different FROM rather than the
    same one with a `1=1` predicate.
    """
    if match is None:
        return "FROM papers p LEFT JOIN paper_texts tx ON tx.paper_id = p.id"
    return """
FROM papers_fts
JOIN papers p ON p.id = papers_fts.rowid
LEFT JOIN paper_texts tx ON tx.paper_id = p.id
"""


def _order_by(sort: SortKey) -> str:
    # Every ordering ends in a unique column so pagination cannot drop or repeat
    # a row when the sort key ties (year is null for plenty of papers).
    if sort == "relevance":
        return "ORDER BY score DESC, p.added_at DESC, p.id DESC"
    if sort == "year":
        return "ORDER BY p.year IS NULL, p.year DESC, p.added_at DESC, p.id DESC"
    return "ORDER BY p.added_at DESC, p.id DESC"


def search_papers(conn: sqlite3.Connection, query: SearchQuery) -> SearchResult:
    """Run a search or a filtered browse. Returns rows plus the unpaged total."""
    match = query.match
    clauses, params = _where(query, match)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cte = _TOPIC_SCOPE_CTE if query.topic_id is not None else ""

    if match is None:
        projection = "p.*, tx.n_pages, NULL AS score, NULL AS snippet"
    else:
        weights = ", ".join(str(w) for w in BM25_WEIGHTS)
        projection = (
            "p.*, tx.n_pages, "
            # bm25() is more negative the better the match; negating it gives a
            # score that sorts and reads the way callers expect.
            f"-bm25(papers_fts, {weights}) AS score, "
            f"snippet(papers_fts, -1, '{_MARK_OPEN}', '{_MARK_CLOSE}', "
            f"'…', {_SNIPPET_TOKENS}) AS snippet"
        )

    sort = resolve_sort(query.sort, has_query=match is not None)
    rows = conn.execute(
        f"{cte} SELECT {projection} {_from_clause(match)} {where} "
        f"{_order_by(sort)} LIMIT :limit OFFSET :offset",
        {**params, "limit": query.limit, "offset": query.offset},
    ).fetchall()

    total = int(
        conn.execute(
            f"{cte} SELECT COUNT(*) AS n {_from_clause(match)} {where}", params
        ).fetchone()["n"]
    )

    return SearchResult(rows=list(rows), total=total, query=query)
