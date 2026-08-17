"""arXiv URL normalization and Atom metadata parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from mouseion.services.arxiv import normalize_arxiv_url, parse_atom

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("value", "expected_id"),
    [
        ("https://arxiv.org/abs/1706.03762", "1706.03762"),
        ("http://arxiv.org/abs/1706.03762", "1706.03762"),
        ("https://www.arxiv.org/abs/1706.03762", "1706.03762"),
        ("https://export.arxiv.org/abs/1706.03762", "1706.03762"),
        ("arxiv.org/abs/1706.03762", "1706.03762"),
        # /pdf/ and .pdf and trailing slashes and query strings
        ("https://arxiv.org/pdf/1706.03762", "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762.pdf", "1706.03762"),
        ("https://arxiv.org/pdf/1706.03762v7.pdf", "1706.03762v7"),
        ("https://arxiv.org/abs/1706.03762/", "1706.03762"),
        ("https://arxiv.org/abs/1706.03762?context=cs", "1706.03762"),
        ("https://arxiv.org/html/2301.00234v2", "2301.00234v2"),
        # five-digit ids
        ("https://arxiv.org/abs/2301.00234", "2301.00234"),
        # pre-2007 identifiers keep their archive/subject-class casing
        ("https://arxiv.org/abs/math.GT/0309136", "math.GT/0309136"),
        ("https://arxiv.org/abs/cs/0112017v1", "cs/0112017v1"),
        # bare ids
        ("1706.03762", "1706.03762"),
        ("arXiv:1706.03762v7", "1706.03762v7"),
    ],
)
def test_normalize_recognises_arxiv(value: str, expected_id: str) -> None:
    ref = normalize_arxiv_url(value)
    assert ref is not None
    assert ref.arxiv_id == expected_id


def test_normalize_rewrites_abs_to_pdf() -> None:
    ref = normalize_arxiv_url("https://arxiv.org/abs/1706.03762v7")
    assert ref is not None
    assert ref.pdf_url == "https://arxiv.org/pdf/1706.03762v7.pdf"
    assert ref.abs_url == "https://arxiv.org/abs/1706.03762v7"
    assert ref.id_without_version == "1706.03762"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://openreview.net/pdf?id=abc123",
        "https://example.com/paper.pdf",
        "https://ieeexplore.ieee.org/document/12345",
        "https://arxiv.org/list/cs.CL/recent",
        "not a url at all",
        # Lookalike host must not be treated as arXiv.
        "https://arxiv.org.evil.example/abs/1706.03762",
    ],
)
def test_normalize_rejects_non_arxiv(value: str) -> None:
    assert normalize_arxiv_url(value) is None


def test_parse_atom_extracts_metadata() -> None:
    metadata = parse_atom((FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8"))

    # arXiv wraps long fields across lines; they must come back as one line.
    assert metadata.title == "Attention Is All You Need"
    assert metadata.authors == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]
    assert metadata.year == 2017
    assert metadata.abstract is not None
    assert metadata.abstract.startswith("The dominant sequence transduction models")
    assert "\n" not in metadata.abstract
    assert metadata.venue == "Advances in Neural Information Processing Systems 30 (2017)"
    assert metadata.doi == "10.48550/arXiv.1706.03762"


def test_parse_atom_raises_on_error_entry() -> None:
    # arXiv answers a bad id with HTTP 200 and an "Error" entry.
    with pytest.raises(ValueError, match="incorrect id format"):
        parse_atom((FIXTURES / "arxiv_error.xml").read_text(encoding="utf-8"))


def test_parse_atom_raises_on_empty_feed() -> None:
    with pytest.raises(ValueError, match="no entry"):
        parse_atom('<feed xmlns="http://www.w3.org/2005/Atom"></feed>')
