"""Search: the FTS query builder, ranking, filters, and recursive topic scope.

The query builder is tested as a pure function because it is the one place a
user's raw keystrokes reach SQL — everything else in the search path is
parameterised.
"""

from __future__ import annotations

import sqlite3

import pytest

from mouseion.services.papers import get_or_create_by_sha256, set_full_text, update_paper
from mouseion.services.search import (
    SearchQuery,
    build_fts_query,
    highlight,
    resolve_sort,
    search_papers,
)
from mouseion.services.taxonomy import get_or_create_topic


# ---------------------------------------------------------------- query builder
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("quantum", '"quantum"*'),
        ("  quantum  ", '"quantum"*'),
        ("quantum error", '"quantum" AND "error"*'),
        # A quoted phrase is an explicit request for that wording, so it is
        # never widened into a prefix search.
        ('"quantum error"', '"quantum error"'),
        ('"quantum error" codes', '"quantum error" AND "codes"*'),
    ],
)
def test_build_fts_query_shapes(raw: str, expected: str) -> None:
    assert build_fts_query(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "***", "-", "()"])
def test_build_fts_query_returns_none_when_there_is_nothing_to_match(raw: str | None) -> None:
    """No terms means browse, not an empty MATCH (which is a syntax error)."""
    assert build_fts_query(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "AND",
        "NOT quantum",
        "quantum OR error",
        "NEAR(a b)",
        'unbalanced "quote',
        "trailing*",
        "a AND (b OR c)",
        "col:value",
        "^anchored",
    ],
)
def test_fts_operators_are_neutralised(conn: sqlite3.Connection, raw: str) -> None:
    """Every FTS5 operator a user might type is quoted into a literal, so the
    query runs instead of raising OperationalError."""
    query = build_fts_query(raw)
    assert query is not None
    # The real proof: SQLite accepts it.
    conn.execute("SELECT rowid FROM papers_fts WHERE papers_fts MATCH ?", (query,)).fetchall()


def test_highlight_escapes_before_marking() -> None:
    """The text around a match is arbitrary PDF content; it must not be able to
    inject markup through the snippet."""
    marked = "a \x02quantum\x03 <script>alert(1)</script>"
    assert highlight(marked) == "a <mark>quantum</mark> &lt;script&gt;alert(1)&lt;/script&gt;"
    assert highlight(None) is None


def test_resolve_sort_falls_back_when_relevance_is_meaningless() -> None:
    assert resolve_sort("relevance", has_query=True) == "relevance"
    assert resolve_sort("relevance", has_query=False) == "added_at"
    assert resolve_sort(None, has_query=True) == "relevance"
    assert resolve_sort(None, has_query=False) == "added_at"
    assert resolve_sort("nonsense", has_query=True) == "relevance"
    assert resolve_sort("year", has_query=False) == "year"


# ---------------------------------------------------------------------- fixtures
def make_paper(
    conn: sqlite3.Connection,
    key: str,
    *,
    title: str,
    abstract: str = "",
    full_text: str = "",
    year: int | None = None,
    status: str = "to_read",
    authors: str = "Ada Lovelace",
    summary_short: str = "",
) -> int:
    paper_id, _ = get_or_create_by_sha256(conn, key * 64)
    update_paper(
        conn,
        paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract or None,
        summary_short=summary_short or None,
        status=status,
    )
    if full_text:
        set_full_text(conn, paper_id, full_text, 1)
    return paper_id


@pytest.fixture
def library(conn: sqlite3.Connection) -> dict[str, int]:
    """Three papers with deliberately different match locations."""
    return {
        "quantum": make_paper(
            conn,
            "a",
            title="Quantum Error Correction",
            abstract="Surface codes protect logical qubits.",
            year=2019,
        ),
        "attention": make_paper(
            conn,
            "b",
            title="Attention Is All You Need",
            abstract="We propose the Transformer.",
            full_text="A passing mention of quantum computing appears here.",
            year=2017,
            status="read",
        ),
        "untitled": make_paper(conn, "c", title="Unrelated Work", year=2021),
    }


def run(conn: sqlite3.Connection, **kwargs) -> list[int]:
    return [row["id"] for row in search_papers(conn, SearchQuery(**kwargs)).rows]


# ------------------------------------------------------------------- searching
def test_search_finds_a_title_match_with_a_snippet(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    result = search_papers(conn, SearchQuery(q="quantum", sort="relevance"))

    assert [row["id"] for row in result.rows] == [library["quantum"], library["attention"]]
    assert result.total == 2
    # A title hit outranks a passing mention in the full text (BM25_WEIGHTS).
    assert result.rows[0]["score"] > result.rows[1]["score"]
    assert "<mark>" in (highlight(result.rows[0]["snippet"]) or "")


def test_prefix_search_matches_the_word_being_typed(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    assert library["quantum"] in run(conn, q="quant")
    assert library["attention"] in run(conn, q="transform")


def test_empty_query_is_a_browse(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    result = search_papers(conn, SearchQuery(q=None))
    assert result.total == 3
    assert all(row["score"] is None and row["snippet"] is None for row in result.rows)


def test_filters_compose(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    assert run(conn, status="read") == [library["attention"]]
    assert set(run(conn, year_from=2018)) == {library["quantum"], library["untitled"]}
    assert run(conn, year_from=2018, year_to=2020) == [library["quantum"]]
    assert run(conn, q="quantum", status="read") == [library["attention"]]


def test_sorting(conn: sqlite3.Connection, library: dict[str, int]) -> None:
    assert run(conn, sort="year") == [
        library["untitled"],
        library["quantum"],
        library["attention"],
    ]


def test_pagination_reports_the_unpaged_total(
    conn: sqlite3.Connection, library: dict[str, int]
) -> None:
    result = search_papers(conn, SearchQuery(limit=2, offset=0))
    assert len(result.rows) == 2
    assert result.total == 3

    second = search_papers(conn, SearchQuery(limit=2, offset=2))
    assert len(second.rows) == 1
    assert second.total == 3


def test_papers_with_no_year_are_excluded_by_a_year_filter(
    conn: sqlite3.Connection,
) -> None:
    """A NULL year is unknown, not "matches every range" — SQL's three-valued
    logic would otherwise drop it silently only for the upper bound."""
    undated = make_paper(conn, "d", title="Undated Work")
    assert undated not in run(conn, year_from=1900)
    assert undated not in run(conn, year_to=2100)
    assert undated in run(conn)


# ------------------------------------------------------- recursive topic scope
def test_topic_filter_includes_grandchildren(conn: sqlite3.Connection) -> None:
    """The headline acceptance case: clicking a mid-tree topic must surface
    papers tagged only with something two levels below it."""
    root, _ = get_or_create_topic(conn, "Machine Learning")
    child, _ = get_or_create_topic(conn, "Architectures", root.id)
    grandchild, _ = get_or_create_topic(conn, "Transformers", child.id)
    sibling, _ = get_or_create_topic(conn, "Optics")

    on_root = make_paper(conn, "a", title="A Survey")
    on_grandchild = make_paper(conn, "b", title="Attention Is All You Need")
    elsewhere = make_paper(conn, "c", title="Lenses")

    conn.executemany(
        "INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)",
        [(on_root, root.id), (on_grandchild, grandchild.id), (elsewhere, sibling.id)],
    )

    assert set(run(conn, topic_id=root.id)) == {on_root, on_grandchild}
    assert run(conn, topic_id=child.id) == [on_grandchild]
    assert run(conn, topic_id=grandchild.id) == [on_grandchild]
    assert run(conn, topic_id=sibling.id) == [elsewhere]


def test_topic_filter_does_not_duplicate_a_paper_tagged_at_two_levels(
    conn: sqlite3.Connection,
) -> None:
    root, _ = get_or_create_topic(conn, "Machine Learning")
    child, _ = get_or_create_topic(conn, "Transformers", root.id)
    paper = make_paper(conn, "a", title="Attention")
    conn.executemany(
        "INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)",
        [(paper, root.id), (paper, child.id)],
    )

    result = search_papers(conn, SearchQuery(topic_id=root.id))
    assert [row["id"] for row in result.rows] == [paper]
    assert result.total == 1


def test_topic_filter_combines_with_a_text_query(conn: sqlite3.Connection) -> None:
    root, _ = get_or_create_topic(conn, "Machine Learning")
    child, _ = get_or_create_topic(conn, "Transformers", root.id)
    hit = make_paper(conn, "a", title="Attention Is All You Need")
    miss = make_paper(conn, "b", title="Attention In Optics")
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (hit, child.id))

    found = run(conn, q="attention", topic_id=root.id)
    assert found == [hit]
    assert miss not in found


def test_a_cycle_cannot_hang_the_topic_scope_query(conn: sqlite3.Connection) -> None:
    """The API refuses to create a cycle, but the search query does not take
    that on trust — the recursive CTE is depth-bounded."""
    a, _ = get_or_create_topic(conn, "A")
    b, _ = get_or_create_topic(conn, "B", a.id)
    conn.execute("UPDATE topics SET parent_id = ? WHERE id = ?", (b.id, a.id))

    paper = make_paper(conn, "a", title="Anything")
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (paper, b.id))

    assert run(conn, topic_id=a.id) == [paper]
