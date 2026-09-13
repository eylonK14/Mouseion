"""Phase 5 operational behavior: health, request ids, and ingest throttling."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from mouseion.services.hashing import sha256_bytes
from mouseion.services.health import HealthCheck, HealthReport, run_full_health
from mouseion.maintenance import create_backup, rebuild_fts, reembed, restore_drill
from mouseion.observability import JsonFormatter, reset_request_id, set_request_id
from mouseion.services.papers import get_or_create_by_sha256, set_full_text, update_paper
from mouseion.services.rate_limit import ingest_limiter

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def ops_client(data_dir, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:  # noqa: ANN001
    from mouseion.services import queue

    async def enqueue(_job_id: str, *, attempt: int = 0) -> None:
        del attempt

    monkeypatch.setattr(queue, "enqueue_ingest", enqueue)
    from mouseion.main import create_app

    with TestClient(create_app()) as client:
        yield client


def test_request_id_is_returned_on_public_and_denied_requests(ops_client: TestClient) -> None:
    supplied = "phone-share-42"
    response = ops_client.get("/health", headers={"X-Request-ID": supplied})
    assert response.headers["x-request-id"] == supplied
    denied = ops_client.get("/api/papers")
    assert denied.status_code == 401
    assert len(denied.headers["x-request-id"]) == 32


def test_json_formatter_includes_request_context() -> None:
    token = set_request_id("format-check")
    try:
        record = logging.LogRecord("mouseion.test", logging.INFO, __file__, 1, "ready", (), None)
        payload = json.loads(JsonFormatter().format(record))
    finally:
        reset_request_id(token)
    assert payload["message"] == "ready"
    assert payload["request_id"] == "format-check"
    assert payload["timestamp"].endswith("Z")


def test_admin_fragment_is_authenticated_and_surfaces_health(
    ops_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_health(**_kwargs) -> HealthReport:  # noqa: ANN003
        return HealthReport(
            "degraded",
            [HealthCheck("pageindex", "warning", "using the heuristic fallback", 3)],
        )

    monkeypatch.setattr("mouseion.api.ui.run_full_health", fake_health)
    assert ops_client.get("/ui/admin").status_code == 401
    response = ops_client.get("/ui/admin", headers=AUTH)
    assert response.status_code == 200
    assert "using the heuristic fallback" in response.text
    assert "Create pairing QR" in response.text


def test_ingest_job_captures_request_id(
    ops_client: TestClient, fixture_pdf_bytes: bytes, conn: sqlite3.Connection
) -> None:
    response = ops_client.post(
        "/api/papers",
        headers={**AUTH, "X-Request-ID": "capture-request"},
        files={"file": ("paper.pdf", fixture_pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 202
    row = conn.execute(
        "SELECT request_id FROM ingest_jobs WHERE id = ?", (response.json()["job_id"],)
    ).fetchone()
    assert row["request_id"] == "capture-request"


def test_ingest_rate_limit_returns_retry_after(
    data_dir, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    from mouseion.config import get_settings

    monkeypatch.setenv("INGEST_RATE_LIMIT_COUNT", "2")
    monkeypatch.setenv("INGEST_RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    ingest_limiter.reset()
    from mouseion.main import create_app

    with TestClient(create_app()) as client:
        for _ in range(2):
            assert client.post("/api/papers", headers=AUTH, content=b"x").status_code == 415
        limited = client.post("/api/papers", headers=AUTH, content=b"x")
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    ingest_limiter.reset()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_full_health_reports_stale_fts_count(
    conn: sqlite3.Connection, settings, stub_embedder
) -> None:  # noqa: ANN001
    paper_id, _ = get_or_create_by_sha256(conn, sha256_bytes(b"health paper"))
    conn.execute("DELETE FROM papers_fts WHERE rowid = ?", (paper_id,))
    report = await run_full_health(
        conn=conn,
        settings=settings,
        embedder=stub_embedder,
        check_external=False,
    )
    fts = next(check for check in report.checks if check.name == "fts")
    assert report.status == "error"
    assert fts.status == "error"
    assert "out of sync" in fts.detail


def test_backup_dry_run_does_not_write(
    settings, tmp_path, conn: sqlite3.Connection
) -> None:  # noqa: ANN001
    get_or_create_by_sha256(conn, sha256_bytes(b"backup paper"))
    backup_dir = tmp_path / "backups"
    configured = settings.model_copy(update={"backup_dir": backup_dir})
    result = create_backup(
        settings=configured,
        dry_run=True,
        now=datetime(2026, 9, 13, 2, 0, tzinfo=UTC),
    )
    assert result.dry_run is True
    assert result.papers == 1
    assert not backup_dir.exists()


@pytest.mark.asyncio
async def test_backup_snapshot_and_restore_drill(
    settings, tmp_path, conn: sqlite3.Connection
) -> None:  # noqa: ANN001
    paper_id, _ = get_or_create_by_sha256(conn, sha256_bytes(b"restorable paper"))
    update_paper(conn, paper_id, title="Restorable Systems")
    set_full_text(conn, paper_id, "durable evidence", 1)
    backup_dir = tmp_path / "backups"
    configured = settings.model_copy(update={"backup_dir": backup_dir})
    result = create_backup(
        settings=configured,
        now=datetime(2026, 9, 13, 2, 5, tzinfo=UTC),
    )
    assert (result.snapshot / "library.db").is_file()
    assert (result.snapshot / "manifest.json").is_file()
    assert await restore_drill(settings=configured) == result.snapshot


def test_reindex_fts_rebuilds_from_source_tables(conn: sqlite3.Connection) -> None:
    paper_id, _ = get_or_create_by_sha256(conn, sha256_bytes(b"fts source"))
    update_paper(conn, paper_id, title="Reindex Sentinel")
    set_full_text(conn, paper_id, "rebuildable full text", 1)
    conn.execute("DELETE FROM papers_fts")
    assert rebuild_fts(conn) == 1
    row = conn.execute(
        "SELECT paper_id FROM papers_fts WHERE papers_fts MATCH ?", ('"sentinel"',)
    ).fetchone()
    assert row["paper_id"] == paper_id


def test_reembed_uses_model_staleness_marker(
    settings, conn: sqlite3.Connection
) -> None:  # noqa: ANN001
    paper_id, _ = get_or_create_by_sha256(conn, sha256_bytes(b"stale vector"))
    update_paper(conn, paper_id, title="Stale Embedding")
    stale, completed = reembed(settings=settings)
    assert (stale, completed) == (1, 1)
    model = conn.execute(
        "SELECT model FROM paper_embeddings WHERE paper_id = ?", (paper_id,)
    ).fetchone()["model"]
    assert model == settings.embedding_model
