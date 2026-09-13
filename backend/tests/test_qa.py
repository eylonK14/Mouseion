"""Phase 3 retrieval, grounding, pipes, streaming API, and cost visibility."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mouseion.services.embeddings import get_embedder, store_embedding
from mouseion.services.llm import LLMMetrics, get_llm_client
from mouseion.services.papers import get_or_create_by_sha256, set_full_text, update_paper
from mouseion.services.qa import (
    PaperGrounding,
    SectionGrounding,
    budget_grounding_material,
    citation_warnings,
    hybrid_candidates,
    reciprocal_rank_fusion,
    resolve_paper_from_text,
)
from mouseion.services.taxonomy import get_or_create_topic
from mouseion.services.tree_indexer import TreeDocument, TreeNode, navigate
from tests.conftest import make_llm_client

AUTH = {"Authorization": "Bearer test-token"}


def _make_paper(
    conn: sqlite3.Connection, key: str, *, title: str, text: str, summary: str
) -> int:
    paper_id, _ = get_or_create_by_sha256(conn, key * 64)
    update_paper(
        conn,
        paper_id,
        title=title,
        abstract=text,
        summary_short=summary,
        summary_long=summary,
    )
    set_full_text(conn, paper_id, text, 1)
    vector = get_embedder().encode(f"{title}\n\n{text}\n\n{summary}")
    assert store_embedding(conn, paper_id, vector, model=get_embedder().model_name)
    return paper_id


def test_rrf_merge_rewards_agreement_and_is_deterministic() -> None:
    merged = reciprocal_rank_fusion([1, 2, 3], [3, 2, 4], limit=4)

    assert [candidate.paper_id for candidate in merged] == [3, 2, 1, 4]
    assert merged[0].vector_rank == 3
    assert merged[0].fts_rank == 1
    assert merged[0].score > merged[2].score


def test_hybrid_retrieval_restricts_to_a_topic_subtree(conn: sqlite3.Connection) -> None:
    root, _ = get_or_create_topic(conn, "Cryptography")
    child, _ = get_or_create_topic(conn, "Lattice Cryptography", root.id)
    other, _ = get_or_create_topic(conn, "Databases")
    inside = _make_paper(
        conn,
        "c",
        title="Cryptography in Lattices",
        text="cryptography exact term",
        summary="A lattice cryptography paper.",
    )
    outside = _make_paper(
        conn,
        "d",
        title="Cryptography in Databases",
        text="cryptography exact term",
        summary="A database paper mentioning cryptography.",
    )
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (inside, child.id))
    conn.execute("INSERT INTO paper_topics (paper_id, topic_id) VALUES (?, ?)", (outside, other.id))

    candidates, topic = hybrid_candidates(
        conn, "cryptography", topic_scope="Cryptography", limit=8
    )

    assert topic == root
    assert [candidate.paper_id for candidate in candidates] == [inside]


def _material(paper_id: int, score: float, size: int) -> PaperGrounding:
    return PaperGrounding(
        paper_id=paper_id,
        title=f"Paper {paper_id}",
        score=score,
        summary_short=None,
        summary_long=None,
        sections=(SectionGrounding("Results", "x" * size),),
    )


def test_context_budget_drops_the_weakest_paper_first() -> None:
    strongest = _material(1, 0.9, 180)
    middle = _material(2, 0.5, 180)
    weakest = _material(3, 0.1, 180)
    budget = strongest.context_chars + middle.context_chars

    kept = budget_grounding_material([strongest, middle, weakest], budget_chars=budget)

    assert [material.paper_id for material in kept] == [1, 2]


async def test_tree_navigation_uses_model_qa_and_returns_only_real_nodes() -> None:
    requests: list[dict] = []
    client = make_llm_client([{"node_ids": ["results", "invented"]}], requests)
    metrics = LLMMetrics()
    tree = TreeDocument(
        sha256="a" * 64,
        indexer="test",
        nodes=[
            TreeNode(node_id="method", title="Method"),
            TreeNode(node_id="results", title="Results", summary="The measured gains."),
        ],
    )

    selected = await navigate(tree, "What gains were measured?", client=client, metrics=metrics)

    assert [node.node_id for node in selected] == ["results"]
    assert requests[0]["model"] == "test/qa-model"
    assert metrics.calls == 1


def test_citation_post_check_flags_only_titles_outside_context() -> None:
    materials = [_material(1, 1.0, 20), _material(2, 0.5, 20)]
    answer = (
        "Supported [Paper 1 §Results], also supported [Paper 2 §Results], "
        "but invented [Imaginary Paper §Method]."
    )

    assert citation_warnings(answer, materials) == ["[Imaginary Paper §Method]"]


def test_fuzzy_title_resolution_locks_only_one_clear_hit(conn: sqlite3.Connection) -> None:
    expected = _make_paper(
        conn,
        "r",
        title="Attention Is All You Need",
        text="transformer attention",
        summary="A transformer paper.",
    )
    _make_paper(
        conn,
        "s",
        title="Lattice Cryptography Handbook",
        text="lattices",
        summary="A cryptography paper.",
    )

    resolution = resolve_paper_from_text(
        conn, "What is the contribution of Attention Is All You Need?"
    )

    assert resolution.locked is not None
    assert resolution.locked.id == expected


def _repository_asset(*parts: str) -> Path:
    """Locate a repository asset in a checkout or the Compose API container."""
    backend_root = Path(__file__).resolve().parents[1]
    for root in (backend_root.parent, backend_root):
        candidate = root.joinpath(*parts)
        if candidate.exists():
            return candidate
    return backend_root.parent.joinpath(*parts)


def _load_pipe_common():  # noqa: ANN202
    path = _repository_asset("pipes", "common.py")
    spec = importlib.util.spec_from_file_location("mouseion_test_pipe_common", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("text", "kind", "status", "value", "rest"),
    [
        ("[paper:12] what's new?", "paper", "present", 12, "what's new?"),
        ("[topic:Cryptography] compare", "topic", "present", "Cryptography", "compare"),
        ("ordinary question", "paper", "absent", None, "ordinary question"),
        ("[paper:twelve] question", "paper", "malformed", None, "[paper:twelve] question"),
        ("[topic:] question", "topic", "malformed", None, "[topic:] question"),
    ],
)
def test_pipe_scope_tag_parsing(text: str, kind: str, status: str, value, rest: str) -> None:
    common = _load_pipe_common()

    parsed = common.parse_scope_tag(text, kind)

    assert (parsed.status, parsed.value, parsed.text) == (status, value, rest)


def test_pipe_paper_lock_survives_followups() -> None:
    common = _load_pipe_common()
    messages = [
        {"role": "user", "content": "Which attention paper did I mean?"},
        {"role": "assistant", "content": "Please choose."},
        {"role": "user", "content": "[paper:12] What's the main contribution?"},
        {"role": "assistant", "content": "It proposes attention."},
        {"role": "user", "content": "What were the limitations?"},
    ]

    cleaned, lock, malformed = common.clean_messages(messages, "paper")
    question, history = common.split_latest_question(cleaned)

    assert malformed is None
    assert lock.value == 12
    assert question == "What were the limitations?"
    assert any(message["content"] == "What's the main contribution?" for message in history)


async def test_pipe_tag_only_selects_paper_without_calling_qa() -> None:
    common = _load_pipe_common()
    body = {"messages": [{"role": "user", "content": "[paper:12]"}]}

    answer = "".join(
        [
            token
            async for token in common.paper_response(
                body, api_base_url="http://unused", token="configured"
            )
        ]
    )

    assert answer == "Paper 12 is selected. What would you like to ask?"


async def test_pipe_tag_only_lock_is_not_forwarded_as_empty_history() -> None:
    common = _load_pipe_common()
    captured: dict = {}

    async def fake_relay_sse(**kwargs):  # noqa: ANN003, ANN202
        captured.update(kwargs)
        yield "grounded answer"

    common.relay_sse = fake_relay_sse
    body = {
        "messages": [
            {"role": "user", "content": "[paper:12]"},
            {
                "role": "assistant",
                "content": "Paper 12 is selected. What would you like to ask?",
            },
            {"role": "user", "content": "How was it evaluated?"},
        ]
    }

    answer = "".join(
        [
            token
            async for token in common.paper_response(
                body, api_base_url="http://unused", token="configured"
            )
        ]
    )

    assert answer == "grounded answer"
    assert captured["path"] == "/api/qa/paper/12"
    assert captured["payload"]["question"] == "How was it evaluated?"
    assert all(message["content"] for message in captured["payload"]["messages"])


def test_paper_chat_deep_link_has_no_built_in_question() -> None:
    source = _repository_asset("frontend", "static", "app.js").read_text(
        encoding="utf-8"
    )

    assert 'url.searchParams.set("q", button.dataset.chatPrefix);' in source
    assert "What's the main contribution?" not in source


class _FakeStream:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.metrics = LLMMetrics(
            model="test/qa-model",
            prompt_tokens=21,
            completion_tokens=9,
            total_tokens=30,
            calls=1,
        )

    async def __aiter__(self):  # noqa: ANN202
        midpoint = len(self.answer) // 2
        yield self.answer[:midpoint]
        yield self.answer[midpoint:]


class _FakeQAClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.stream_calls = 0

    def stream_text(self, **_kwargs):  # noqa: ANN003, ANN202
        self.stream_calls += 1
        return _FakeStream(self.answer)


@pytest.fixture
def qa_paper(conn: sqlite3.Connection) -> int:
    return _make_paper(
        conn,
        "q",
        title="Quantum Error Correction",
        text="Surface codes protect logical qubits from quantum noise.",
        summary="Surface codes encode a logical qubit across physical qubits.",
    )


@pytest.fixture
def qa_client(
    data_dir, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, _FakeQAClient]]:
    from mouseion.api import ui
    from mouseion.main import create_app

    fake = _FakeQAClient(
        "Surface codes distribute information across physical qubits "
        "[Quantum Error Correction §Library summary]."
    )
    ui.reset_templates()
    app = create_app()
    app.dependency_overrides[get_llm_client] = lambda: fake
    with TestClient(app) as client:
        yield client, fake


def test_collection_qa_streams_metadata_and_logs_usage(
    qa_client: tuple[TestClient, _FakeQAClient], conn: sqlite3.Connection, qa_paper: int
) -> None:
    client, fake = qa_client

    response = client.post(
        "/api/qa/collection",
        headers=AUTH,
        json={
            "question": "How do surface codes protect a quantum qubit?",
            "messages": [{"role": "user", "content": "Keep it concise."}],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'event: metadata' in response.text
    assert f'"id": {qa_paper}' in response.text
    assert '"hit": {"paper": {"id":' in response.text
    assert "event: token" in response.text
    assert '"citations_valid": true' in response.text
    assert fake.stream_calls == 1

    log = conn.execute("SELECT * FROM qa_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["scope"] == "collection"
    assert log["model"] == "test/qa-model"
    assert (log["prompt_tokens"], log["completion_tokens"], log["total_tokens"]) == (
        21,
        9,
        30,
    )
    assert log["latency_ms"] >= 0
    assert '"title": "Quantum Error Correction"' in log["consulted_papers_json"]


def test_collection_qa_consults_multiple_matching_papers(
    qa_client: tuple[TestClient, _FakeQAClient], conn: sqlite3.Connection, qa_paper: int
) -> None:
    client, _ = qa_client
    second = _make_paper(
        conn,
        "m",
        title="Fault-Tolerant Surface Codes",
        text="Surface codes protect quantum information with repeated checks.",
        summary="Repeated checks reveal errors without measuring the logical state.",
    )

    response = client.post(
        "/api/qa/collection",
        headers=AUTH,
        json={"question": "How do surface codes protect quantum information?"},
    )

    assert response.status_code == 200
    assert f'"id": {qa_paper}' in response.text
    assert f'"id": {second}' in response.text


def test_paper_qa_is_scoped(
    qa_client: tuple[TestClient, _FakeQAClient], qa_paper: int
) -> None:
    client, _ = qa_client
    scoped = client.post(
        f"/api/qa/paper/{qa_paper}",
        headers=AUTH,
        json={"messages": [{"role": "user", "content": "What's the contribution?"}]},
    )
    assert scoped.status_code == 200
    assert '"scope": "paper"' in scoped.text
    assert "Quantum Error Correction" in scoped.text


def test_paper_title_resolution_endpoint(
    qa_client: tuple[TestClient, _FakeQAClient], qa_paper: int
) -> None:
    client, _ = qa_client

    response = client.post(
        "/api/qa/paper/resolve",
        headers=AUTH,
        json={"text": "Explain the Quantum Error Correction paper"},
    )

    assert response.status_code == 200
    assert response.json()["locked"]["id"] == qa_paper


def test_empty_collection_explicitly_abstains(
    qa_client: tuple[TestClient, _FakeQAClient], conn: sqlite3.Connection
) -> None:
    client, fake = qa_client
    missing = client.post(
        "/api/qa/collection",
        headers=AUTH,
        json={"question": "What does the library say about marine archaeology?"},
    )

    assert missing.status_code == 200
    assert "The answer is not in your library." in missing.text
    assert fake.stream_calls == 0
    log = conn.execute("SELECT * FROM qa_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["candidate_count"] == 0
