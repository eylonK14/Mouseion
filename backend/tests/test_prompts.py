"""Prompt builder — golden file plus the invariants that matter.

The golden file pins the exact bytes for a known taxonomy and a known paper.
If you change the wording, regenerate it deliberately:

    python -m tests.regenerate_golden      (from backend/, with the venv active)
"""

from __future__ import annotations

from pathlib import Path

from mouseion.services.prompts import (
    EMPTY_TAXONOMY,
    PROMPT_VERSION,
    GroundingExcerpt,
    QA_PROMPT_VERSION,
    bound_paper_text,
    build_grounding_prompt,
    build_ingest_prompt,
    extract_section_headers,
    render_taxonomy_tree,
)
from mouseion.services.taxonomy import TopicRow

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "ingest_prompt.golden.txt"
QA_GOLDEN = FIXTURES / "grounding_prompt.golden.txt"

TAXONOMY = [
    TopicRow(id=1, name="Machine Learning", parent_id=None),
    TopicRow(id=2, name="Information Retrieval", parent_id=None),
    TopicRow(id=3, name="Transformers", parent_id=1),
    TopicRow(id=4, name="Attention Mechanisms", parent_id=3),
    TopicRow(id=5, name="Dense Retrieval", parent_id=2),
]

PAPER_TEXT = (
    "A Tiny Paper About Attention\n"
    "Ada Lovelace, Alan Turing\n"
    "Abstract\n"
    "We study a small thing carefully.\n"
    "1 Introduction\n"
    "Attention lets a model weigh its inputs.\n"
    "2 Method\n"
    "We compute scaled dot products.\n"
)


def build_golden_prompt() -> str:
    prompt = build_ingest_prompt(
        paper_text=PAPER_TEXT,
        topics=TAXONOMY,
        known_metadata={"title": "A Tiny Paper About Attention", "year": 2017},
        missing_metadata=["authors", "venue", "abstract"],
        text_budget=24_000,
    )
    return f"{prompt.system}\n===== USER =====\n{prompt.user}"


def build_qa_golden_prompt() -> str:
    prompt = build_grounding_prompt(
        question="How do the papers characterize attention?",
        excerpts=[
            GroundingExcerpt(
                paper_id=7,
                paper_title="Attention Is All You Need",
                section="3.2 Attention",
                text="Scaled dot-product attention maps queries and keys to weighted values.",
            ),
            GroundingExcerpt(
                paper_id=11,
                paper_title="Attention Revisited",
                section="Limitations",
                text="The reported gains do not persist on the smallest evaluation split.",
            ),
        ],
    )
    return f"{prompt.system}\n===== USER =====\n{prompt.user}"


def test_prompt_matches_golden_file() -> None:
    assert GOLDEN.exists(), "golden file missing — run tests/regenerate_golden.py"
    assert build_golden_prompt() == GOLDEN.read_text(encoding="utf-8")


def test_grounding_prompt_matches_golden_file() -> None:
    assert QA_GOLDEN.exists(), "golden file missing — run tests/regenerate_golden.py"
    assert build_qa_golden_prompt() == QA_GOLDEN.read_text(encoding="utf-8")
    assert QA_PROMPT_VERSION in build_qa_golden_prompt()


def test_prompt_contains_the_rendered_taxonomy_tree() -> None:
    prompt = build_golden_prompt()
    # Every topic appears with its id, indented under its parent.
    assert "- [1] Machine Learning" in prompt
    assert "  - [3] Transformers" in prompt
    assert "    - [4] Attention Mechanisms" in prompt
    assert "- [2] Information Retrieval" in prompt
    assert "  - [5] Dense Retrieval" in prompt


def test_prompt_states_the_pick_or_propose_contract() -> None:
    prompt = build_golden_prompt()
    assert "PICK an existing topic" in prompt
    assert "PROPOSE a new topic" in prompt
    assert "existing_topic_id" in prompt
    assert "new_topic_name" in prompt
    # Drift control: free-form tags and near-duplicates are forbidden by name.
    assert "NEVER invent free-form tags" in prompt
    assert "NEVER propose a name that is a near-duplicate" in prompt
    assert PROMPT_VERSION in prompt


def test_prompt_separates_known_from_missing_metadata() -> None:
    prompt = build_golden_prompt()
    assert "## METADATA ALREADY KNOWN (authoritative — do not contradict)" in prompt
    assert "- title: A Tiny Paper About Attention" in prompt
    assert "## METADATA YOU MUST SUPPLY" in prompt
    assert "- authors" in prompt


def test_render_taxonomy_tree_handles_empty_library() -> None:
    assert render_taxonomy_tree([]) == EMPTY_TAXONOMY
    prompt = build_ingest_prompt(paper_text="x", topics=[])
    assert EMPTY_TAXONOMY in prompt.user


def test_render_taxonomy_tree_is_deterministic_regardless_of_row_order() -> None:
    forward = render_taxonomy_tree(TAXONOMY)
    backward = render_taxonomy_tree(list(reversed(TAXONOMY)))
    assert forward == backward


def test_render_taxonomy_tree_keeps_orphans_visible() -> None:
    # A topic whose parent was deleted must still be shown, at the root.
    rendered = render_taxonomy_tree([TopicRow(id=7, name="Stranded", parent_id=999)])
    assert rendered == "- [7] Stranded"


def test_extract_section_headers_finds_numbered_and_named_sections() -> None:
    headers = extract_section_headers(
        "1 Introduction\nsome prose here\n3.2 Ablations\nReferences\nnot a header at all\n"
    )
    assert "1 Introduction" in headers
    assert "3.2 Ablations" in headers
    assert "References" in headers
    assert "not a header at all" not in headers


def test_bound_paper_text_passes_short_papers_through() -> None:
    assert bound_paper_text(PAPER_TEXT, 24_000) == PAPER_TEXT.strip()


def test_bound_paper_text_keeps_head_and_section_headers() -> None:
    long_text = ("head padding. " * 500) + "\n3 Method\nbody\n7 Conclusion\nend\n"
    bounded = bound_paper_text(long_text, 1000)

    assert len(bounded) < len(long_text)
    assert bounded.startswith("head padding.")
    assert "[... middle of the paper omitted ...]" in bounded
    assert "SECTION HEADINGS FROM THE OMITTED PART:" in bounded
    assert "- 3 Method" in bounded
    assert "- 7 Conclusion" in bounded
