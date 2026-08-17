"""API surface: auth, the two ingest shapes, dedup, pagination."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mouseion.services.hashing import sha256_bytes
from mouseion.services.papers import get_or_create_by_sha256, update_paper

AUTH = {"Authorization": "Bearer test-token"}


class _Captured(list):
    """List of enqueued job ids, plus the attempt number each was queued under."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[int] = []


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    """Capture enqueues instead of reaching Redis."""
    from mouseion.services import queue

    captured = _Captured()

    async def fake_enqueue(job_id: str, *, attempt: int = 0) -> None:
        captured.append(job_id)
        captured.attempts.append(attempt)

    monkeypatch.setattr(queue, "enqueue_ingest", fake_enqueue)
    return captured


@pytest.fixture
def client(data_dir, enqueued: list[str]) -> Iterator[TestClient]:
    # Imported after the environment is configured so create_app() reads it.
    from mouseion.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# ---------------------------------------------------------------- auth
def test_health_is_public(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["tree_indexer"] == "heuristic"


def test_api_requires_a_token(client: TestClient) -> None:
    assert client.get("/api/papers").status_code == 401
    assert client.get("/api/papers", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/papers", headers={"Authorization": "test-token"}).status_code == 401
    assert client.get("/api/papers", headers=AUTH).status_code == 200


def test_openapi_is_not_public(client: TestClient) -> None:
    # Only /health and the static shell are exempt; the schema is not.
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers=AUTH).status_code == 200


# -------------------------------------------------------------- upload
def test_upload_accepts_a_pdf_and_queues_a_job(
    client: TestClient, fixture_pdf_bytes: bytes, enqueued: list[str], conn: sqlite3.Connection
) -> None:
    response = client.post(
        "/api/papers",
        headers=AUTH,
        files={"file": ("tiny.pdf", fixture_pdf_bytes, "application/pdf")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["duplicate"] is False
    assert body["paper_id"] is None
    assert enqueued == [body["job_id"]]

    # The bytes are stored under their hash before the worker ever runs.
    job = client.get(f"/api/jobs/{body['job_id']}", headers=AUTH).json()
    assert job["state"] == "queued"
    assert job["sha256"] == sha256_bytes(fixture_pdf_bytes)


def test_reupload_of_a_known_hash_short_circuits_as_duplicate(
    client: TestClient, fixture_pdf_bytes: bytes, conn: sqlite3.Connection, enqueued: list[str]
) -> None:
    digest = sha256_bytes(fixture_pdf_bytes)
    paper_id, _ = get_or_create_by_sha256(conn, digest)

    response = client.post(
        "/api/papers",
        headers=AUTH,
        files={"file": ("tiny.pdf", fixture_pdf_bytes, "application/pdf")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["duplicate"] is True
    assert body["paper_id"] == paper_id
    # A duplicate costs nothing: no worker job is queued...
    assert enqueued == []
    # ...but it still gets a job row, so the UI has something to poll.
    assert client.get(f"/api/jobs/{body['job_id']}", headers=AUTH).json()["state"] == "done"


def test_upload_rejects_a_non_pdf(client: TestClient) -> None:
    response = client.post(
        "/api/papers",
        headers=AUTH,
        files={"file": ("notes.html", b"<!doctype html><html>nope</html>", "text/html")},
    )
    assert response.status_code == 400
    assert "not a PDF" in response.json()["detail"]


def test_upload_rejects_an_empty_file(client: TestClient) -> None:
    response = client.post(
        "/api/papers", headers=AUTH, files={"file": ("empty.pdf", b"", "application/pdf")}
    )
    assert response.status_code == 400


def test_upload_enforces_the_size_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mouseion.config import get_settings

    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()

    oversized = b"%PDF-1.7\n" + b"0" * (2 * 1024 * 1024)
    response = client.post(
        "/api/papers", headers=AUTH, files={"file": ("big.pdf", oversized, "application/pdf")}
    )
    assert response.status_code == 413


# ----------------------------------------------------------------- url
def test_url_ingest_queues_a_job(client: TestClient, enqueued: list[str]) -> None:
    response = client.post(
        "/api/papers", headers=AUTH, json={"url": "https://arxiv.org/abs/1706.03762"}
    )

    assert response.status_code == 202
    assert response.json()["duplicate"] is False
    assert len(enqueued) == 1


def test_resubmitting_a_known_arxiv_url_short_circuits(
    client: TestClient, conn: sqlite3.Connection, enqueued: list[str]
) -> None:
    paper_id, _ = get_or_create_by_sha256(conn, "c" * 64)
    update_paper(conn, paper_id, source_url="https://arxiv.org/abs/1706.03762")

    # A different spelling of the same paper still resolves to the same URL.
    response = client.post(
        "/api/papers", headers=AUTH, json={"url": "https://arxiv.org/pdf/1706.03762.pdf"}
    )

    body = response.json()
    assert body["duplicate"] is True
    assert body["paper_id"] == paper_id
    assert enqueued == []


@pytest.mark.parametrize(
    "payload", [{}, {"url": ""}, {"url": "ftp://example.com/a.pdf"}, {"url": "file:///etc/passwd"}]
)
def test_url_ingest_rejects_bad_input(client: TestClient, payload: dict) -> None:
    assert client.post("/api/papers", headers=AUTH, json=payload).status_code == 400


def test_unsupported_content_type(client: TestClient) -> None:
    response = client.post(
        "/api/papers", headers={**AUTH, "Content-Type": "text/plain"}, content=b"hello"
    )
    assert response.status_code == 415


# ---------------------------------------------------------------- read
def test_list_and_detail(client: TestClient, conn: sqlite3.Connection) -> None:
    paper_id, _ = get_or_create_by_sha256(conn, "d" * 64)
    update_paper(
        conn,
        paper_id,
        title="A Tiny Paper",
        authors="Ada Lovelace; Alan Turing",
        year=2017,
        summary_short="It does a thing.",
    )

    listed = client.get("/api/papers", headers=AUTH).json()
    assert listed["total"] == 1
    assert listed["items"][0]["title"] == "A Tiny Paper"
    # The "; "-joined column comes back as a list at the API boundary.
    assert listed["items"][0]["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert listed["items"][0]["status"] == "to_read"

    detail = client.get(f"/api/papers/{paper_id}", headers=AUTH).json()
    assert detail["id"] == paper_id
    assert detail["topics"] == []


def test_list_paginates(client: TestClient, conn: sqlite3.Connection) -> None:
    for index in range(5):
        get_or_create_by_sha256(conn, f"{index:064d}")

    page = client.get("/api/papers?limit=2&offset=2", headers=AUTH).json()
    assert page["total"] == 5
    assert len(page["items"]) == 2
    assert page["limit"] == 2 and page["offset"] == 2


def test_unknown_ids_are_404(client: TestClient) -> None:
    assert client.get("/api/papers/999", headers=AUTH).status_code == 404
    assert client.get("/api/jobs/nope", headers=AUTH).status_code == 404


def test_retry_only_applies_to_failed_jobs(
    client: TestClient, conn: sqlite3.Connection, enqueued: list[str]
) -> None:
    from mouseion.services.jobs import JobKind, JobState, create_job, set_state

    job_id = create_job(conn, JobKind.URL, {"url": "https://example.com/a.pdf"})
    assert client.post(f"/api/jobs/{job_id}/retry", headers=AUTH).status_code == 409

    set_state(conn, job_id, JobState.FAILED, error="boom")
    response = client.post(f"/api/jobs/{job_id}/retry", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["state"] == "queued"
    # The error is cleared on requeue rather than lingering on a live job.
    assert response.json()["error"] is None
    assert enqueued == [job_id]


def test_retry_enqueues_under_a_fresh_attempt_number(
    client: TestClient, conn: sqlite3.Connection, enqueued: _Captured
) -> None:
    """Regression: arq drops a re-enqueue that reuses a job id whose result it
    still retains, so a retry must not queue under the same attempt as the run
    that failed. Symptom was a job stuck in `queued` forever."""
    from mouseion.services.jobs import JobKind, JobState, bump_attempts, create_job, set_state

    job_id = create_job(conn, JobKind.URL, {"url": "https://example.com/a.pdf"})
    bump_attempts(conn, job_id)  # the failed run
    set_state(conn, job_id, JobState.FAILED, error="boom")

    assert client.post(f"/api/jobs/{job_id}/retry", headers=AUTH).status_code == 200
    assert enqueued.attempts == [1]  # not 0, which the failed run already used


def test_first_submit_enqueues_under_attempt_zero(
    client: TestClient, fixture_pdf_bytes: bytes, enqueued: _Captured
) -> None:
    client.post(
        "/api/papers",
        headers=AUTH,
        files={"file": ("tiny.pdf", fixture_pdf_bytes, "application/pdf")},
    )
    assert enqueued.attempts == [0]


def test_a_refused_enqueue_is_reported_not_swallowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fixture_pdf_bytes: bytes
) -> None:
    """Regression: arq returns None instead of raising when it refuses a job.
    Treating that as success left the UI showing `queued` for work that would
    never run."""
    from mouseion.services import queue

    async def refusing_enqueue(job_id: str, *, attempt: int = 0) -> None:
        raise queue.EnqueueError("already running")

    monkeypatch.setattr(queue, "enqueue_ingest", refusing_enqueue)

    response = client.post(
        "/api/papers",
        headers=AUTH,
        files={"file": ("tiny.pdf", fixture_pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 503
