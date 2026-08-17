"""End-to-end ingest with the LLM mocked and a tiny fixture PDF.

Everything except the network and the embedding model is real here: the real
schema, the real FTS triggers, the real heuristic tree indexer, the real
pipeline and its state machine.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from mouseion.services import papers as papers_repo
from mouseion.services.ingest import run_ingest
from mouseion.services.jobs import JobKind, Step, create_job, get_job, get_progress
from mouseion.services.pdfs import store_pdf
from mouseion.services.taxonomy import list_topics, topics_for_paper
from tests.conftest import extraction_payload, make_llm_client

FIXTURES = Path(__file__).parent / "fixtures"
ALL_STEPS = [s.value for s in (Step.DOWNLOAD, Step.EXTRACT, Step.INDEX, Step.TAG, Step.EMBED)]


def make_upload_job(conn: sqlite3.Connection, settings, data: bytes) -> tuple[str, str]:
    _, digest = store_pdf(data, settings.pdf_dir)
    job_id = create_job(conn, JobKind.UPLOAD, {"filename": "tiny.pdf", "sha256": digest})
    return job_id, digest


def arxiv_client(pdf_bytes: bytes) -> httpx.AsyncClient:
    atom = (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "export.arxiv.org/api/query" in url:
            assert "id_list=1706.03762" in url
            return httpx.Response(200, text=atom)
        if url.startswith("https://arxiv.org/pdf/"):
            return httpx.Response(
                200, content=pdf_bytes, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404, text="not found")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------
async def test_upload_ingest_populates_everything(
    conn: sqlite3.Connection, settings, fixture_pdf_bytes: bytes
) -> None:
    job_id, digest = make_upload_job(conn, settings, fixture_pdf_bytes)
    requests: list[dict] = []
    llm = make_llm_client(
        [
            extraction_payload(
                topics=[{"new_topic_name": "Attention Mechanisms", "parent": None}],
                title="A Tiny Paper About Attention",
                authors=["Ada Lovelace", "Alan Turing"],
                year=2024,
                venue="NeurIPS",
                abstract="We study a small thing carefully.",
            )
        ],
        requests,
    )

    outcome = await run_ingest(job_id, conn=conn, settings=settings, llm=llm)

    assert outcome.duplicate is False
    assert outcome.paper_id is not None
    assert outcome.sha256 == digest

    job = get_job(conn, job_id)
    assert job["state"] == "done"
    assert job["error"] is None
    assert get_progress(conn, job_id)["steps"] == ALL_STEPS

    paper = papers_repo.get_paper(conn, outcome.paper_id)
    assert paper["title"] == "A Tiny Paper About Attention"
    assert paper["authors"] == "Ada Lovelace; Alan Turing"
    assert paper["year"] == 2024
    assert paper["venue"] == "NeurIPS"
    assert paper["summary_short"] == "It computes attention over inputs."
    assert paper["summary_long"].startswith("The paper introduces")
    assert paper["status"] == "to_read"  # default reading state

    assert [t.name for t in topics_for_paper(conn, outcome.paper_id)] == ["Attention Mechanisms"]

    # The prompt actually carried the contract and the (empty) taxonomy.
    sent = requests[0]["messages"][1]["content"]
    assert "## CURRENT TAXONOMY" in sent
    assert "empty" in sent
    assert "PICK an existing topic" in requests[0]["messages"][0]["content"]

    # Full text landed, and the FTS triggers indexed it without a backfill.
    text = papers_repo.get_full_text(conn, outcome.paper_id)
    assert text["n_pages"] == 3
    assert "scaled dot products" in text["full_text"]
    hits = conn.execute(
        "SELECT paper_id FROM papers_fts WHERE papers_fts MATCH 'scaled'"
    ).fetchall()
    assert [row["paper_id"] for row in hits] == [outcome.paper_id]

    # A summary written after the paper row still reaches the index.
    hits = conn.execute(
        "SELECT paper_id FROM papers_fts WHERE papers_fts MATCH 'recurrence'"
    ).fetchall()
    assert [row["paper_id"] for row in hits] == [outcome.paper_id]

    # Tree persisted where Phase 3 will look for it.
    tree_file = settings.tree_dir / f"{digest}.json"
    assert tree_file.exists()
    tree = json.loads(tree_file.read_text(encoding="utf-8"))
    assert tree["sha256"] == digest
    assert tree["indexer"] == "heuristic"
    assert tree["nodes"]

    # Embedding stored with the model name recorded for later re-embedding.
    embedding = conn.execute(
        "SELECT model, dim, source FROM paper_embeddings WHERE paper_id = ?",
        (outcome.paper_id,),
    ).fetchone()
    assert embedding["model"] == "test/embedder"
    assert embedding["dim"] == 384
    assert embedding["source"] == "title+abstract+topics+summary_long"
    vectors = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_vectors WHERE paper_id = ?", (outcome.paper_id,)
    ).fetchone()
    assert vectors["n"] == 1


async def test_second_paper_reuses_the_topic_the_first_created(
    conn: sqlite3.Connection, settings, fixture_pdf_bytes: bytes, other_pdf: Path
) -> None:
    first_job, _ = make_upload_job(conn, settings, fixture_pdf_bytes)
    first_llm = make_llm_client(
        [extraction_payload(topics=[{"new_topic_name": "Attention Mechanisms", "parent": None}])]
    )
    first = await run_ingest(first_job, conn=conn, settings=settings, llm=first_llm)

    topic = list_topics(conn)[0]
    assert topic.name == "Attention Mechanisms"

    # Second paper, same subject: the model is shown the taxonomy and picks it.
    second_job, _ = make_upload_job(conn, settings, other_pdf.read_bytes())
    requests: list[dict] = []
    second_llm = make_llm_client(
        [extraction_payload(topics=[{"existing_topic_id": topic.id}])], requests
    )
    second = await run_ingest(second_job, conn=conn, settings=settings, llm=second_llm)

    assert second.paper_id != first.paper_id
    # The taxonomy did not grow, and both papers hang off the same topic.
    assert len(list_topics(conn)) == 1
    assert [t.id for t in topics_for_paper(conn, second.paper_id)] == [topic.id]
    # The second prompt showed the topic that now exists.
    assert f"- [{topic.id}] Attention Mechanisms" in requests[0]["messages"][1]["content"]


async def test_reingesting_the_same_bytes_short_circuits_as_duplicate(
    conn: sqlite3.Connection, settings, fixture_pdf_bytes: bytes
) -> None:
    first_job, digest = make_upload_job(conn, settings, fixture_pdf_bytes)
    await run_ingest(
        first_job,
        conn=conn,
        settings=settings,
        llm=make_llm_client([extraction_payload(topics=[{"new_topic_name": "Attention"}])]),
    )

    second_job = create_job(conn, JobKind.UPLOAD, {"filename": "again.pdf", "sha256": digest})
    requests: list[dict] = []
    outcome = await run_ingest(
        second_job, conn=conn, settings=settings, llm=make_llm_client([], requests)
    )

    assert outcome.duplicate is True
    assert papers_repo.count_papers(conn) == 1
    assert get_job(conn, second_job)["state"] == "done"
    assert bool(get_job(conn, second_job)["duplicate"]) is True
    # A duplicate must not spend a single LLM call.
    assert requests == []


# ---------------------------------------------------------------------------
# arXiv URL
# ---------------------------------------------------------------------------
async def test_arxiv_url_ingest_uses_api_metadata_over_the_model(
    conn: sqlite3.Connection, settings, fixture_pdf_bytes: bytes
) -> None:
    job_id = create_job(conn, JobKind.URL, {"url": "https://arxiv.org/abs/1706.03762"})
    requests: list[dict] = []
    # The model tries to supply metadata; arXiv's must win anyway.
    llm = make_llm_client(
        [
            extraction_payload(
                topics=[{"new_topic_name": "Transformers"}],
                title="WRONG TITLE FROM THE MODEL",
                authors=["Nobody At All"],
                year=1999,
            )
        ],
        requests,
    )

    async with arxiv_client(fixture_pdf_bytes) as http_client:
        outcome = await run_ingest(
            job_id, conn=conn, settings=settings, http_client=http_client, llm=llm
        )

    paper = papers_repo.get_paper(conn, outcome.paper_id)
    assert paper["title"] == "Attention Is All You Need"
    assert paper["authors"] == "Ashish Vaswani; Noam Shazeer; Niki Parmar"
    assert paper["year"] == 2017
    assert paper["venue"] == "Advances in Neural Information Processing Systems 30 (2017)"
    assert paper["abstract"].startswith("The dominant sequence transduction models")
    # /abs/ is the canonical source we record, even though we fetched /pdf/.
    assert paper["source_url"] == "https://arxiv.org/abs/1706.03762"
    # Summaries still come from the model.
    assert paper["summary_short"] == "It computes attention over inputs."

    # And the prompt told the model there was nothing left to supply.
    sent = requests[0]["messages"][1]["content"]
    assert "(none — every field is already known" in sent
    assert "- title: Attention Is All You Need" in sent


async def test_non_pdf_url_fails_with_a_clear_message(
    conn: sqlite3.Connection, settings
) -> None:
    job_id = create_job(conn, JobKind.URL, {"url": "https://example.com/paper-landing-page"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<!doctype html><html>paywall</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(Exception, match="did not return a PDF"):
            await run_ingest(job_id, conn=conn, settings=settings, http_client=http_client)

    job = get_job(conn, job_id)
    assert job["state"] == "failed"
    assert "did not return a PDF" in job["error"]
    assert "upload the file instead" in job["error"]
    assert papers_repo.count_papers(conn) == 0


# ---------------------------------------------------------------------------
# failure and resume
# ---------------------------------------------------------------------------
class BoomEmbedder:
    model_name = "test/embedder"
    dim = 384

    def encode(self, text: str) -> list[float]:
        raise RuntimeError("model exploded")


async def test_failed_job_resumes_from_the_last_completed_step(
    conn: sqlite3.Connection, settings, fixture_pdf_bytes: bytes, stub_embedder
) -> None:
    from mouseion.services import embeddings

    job_id, _ = make_upload_job(conn, settings, fixture_pdf_bytes)
    requests: list[dict] = []
    llm = make_llm_client(
        [extraction_payload(topics=[{"new_topic_name": "Attention Mechanisms"}])], requests
    )

    embeddings.set_embedder(BoomEmbedder())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="model exploded"):
        await run_ingest(job_id, conn=conn, settings=settings, llm=llm)

    job = get_job(conn, job_id)
    assert job["state"] == "failed"
    assert "model exploded" in job["error"]
    # Everything up to embedding is banked.
    steps = get_progress(conn, job_id)["steps"]
    assert steps == ALL_STEPS[:-1]
    assert len(requests) == 1

    # Resume: only the embed step should run again.
    embeddings.set_embedder(stub_embedder)  # type: ignore[arg-type]
    outcome = await run_ingest(job_id, conn=conn, settings=settings, llm=llm)

    job = get_job(conn, job_id)
    assert job["state"] == "done"
    assert job["error"] is None
    assert job["attempts"] == 2
    assert get_progress(conn, job_id)["steps"] == ALL_STEPS
    # The expensive call was NOT repeated — that is the point of resuming.
    assert len(requests) == 1
    assert papers_repo.count_papers(conn) == 1

    embedding = conn.execute(
        "SELECT model FROM paper_embeddings WHERE paper_id = ?", (outcome.paper_id,)
    ).fetchone()
    assert embedding["model"] == "test/embedder"


async def test_running_a_completed_job_again_is_harmless(
    conn: sqlite3.Connection, settings, fixture_pdf_bytes: bytes
) -> None:
    job_id, _ = make_upload_job(conn, settings, fixture_pdf_bytes)
    requests: list[dict] = []
    llm = make_llm_client(
        [extraction_payload(topics=[{"new_topic_name": "Attention Mechanisms"}])], requests
    )

    first = await run_ingest(job_id, conn=conn, settings=settings, llm=llm)
    second = await run_ingest(job_id, conn=conn, settings=settings, llm=llm)

    assert second.paper_id == first.paper_id
    assert papers_repo.count_papers(conn) == 1
    assert len(list_topics(conn)) == 1
    assert len(requests) == 1
    assert get_job(conn, job_id)["state"] == "done"
