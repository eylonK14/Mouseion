"""Shared fixtures.

Two rules the whole suite relies on:

* No network, no real model. Every outbound seam (OpenRouter, arXiv, PDF
  download, sentence-transformers) is injected.
* The schema under test is the real one — the template database is built by
  running Alembic, not by hand-written DDL that could drift from migration 0001.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------
def _configure_env(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    from mouseion.config import get_settings

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("DB_PATH", str(data_dir / "library.db"))
    monkeypatch.setenv("API_TOKEN", "test-token")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_INGEST", "test/ingest-model")
    monkeypatch.setenv("MODEL_QA", "test/qa-model")
    monkeypatch.setenv("EMBEDDING_MODEL", "test/embedder")
    monkeypatch.setenv("EMBEDDING_DIM", str(EMBEDDING_DIM))
    monkeypatch.setenv("TREE_INDEXER", "heuristic")
    monkeypatch.setenv("FRONTEND_DIR", str(BACKEND_ROOT.parent / "frontend"))
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated database, built once, copied per test."""
    from alembic import command
    from alembic.config import Config

    from mouseion.config import get_settings

    data_dir = tmp_path_factory.mktemp("template")
    with pytest.MonkeyPatch.context() as monkeypatch:
        _configure_env(monkeypatch, data_dir)
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        command.upgrade(config, "head")
    get_settings.cache_clear()
    return data_dir / "library.db"


@pytest.fixture
def data_dir(tmp_path: Path, template_db: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "data"
    (target / "pdfs").mkdir(parents=True)
    (target / "trees").mkdir(parents=True)
    shutil.copy(template_db, target / "library.db")
    _configure_env(monkeypatch, target)
    return target


@pytest.fixture
def settings(data_dir: Path):  # noqa: ANN201
    from mouseion.config import get_settings

    return get_settings()


@pytest.fixture
def conn(data_dir: Path) -> Iterator[sqlite3.Connection]:
    from mouseion.db import connect

    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# fixture PDF
# ---------------------------------------------------------------------------
def _write_pdf(path: Path, title: str, body_pages: list[str]) -> Path:
    import pymupdf

    doc = pymupdf.open()
    first = doc.new_page()
    first.insert_text((72, 96), title, fontsize=16)
    first.insert_text((72, 130), "Ada Lovelace, Alan Turing", fontsize=10)
    first.insert_text((72, 160), "Abstract", fontsize=12)
    first.insert_text((72, 180), "We study a small thing carefully.", fontsize=10)
    for text in body_pages:
        page = doc.new_page()
        for offset, line in enumerate(text.splitlines()):
            page.insert_text((72, 96 + offset * 16), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def fixture_pdf(tmp_path: Path) -> Path:
    """A tiny two-page PDF with a title, authors, an abstract and sections."""
    return _write_pdf(
        tmp_path / "tiny.pdf",
        "A Tiny Paper About Attention",
        [
            "1 Introduction\nAttention lets a model weigh its inputs.\n"
            "2 Method\nWe compute scaled dot products.",
            "3 Results\nIt works on the toy task.\n4 Conclusion\nMore work is needed.",
        ],
    )


@pytest.fixture
def fixture_pdf_bytes(fixture_pdf: Path) -> bytes:
    return fixture_pdf.read_bytes()


@pytest.fixture
def other_pdf(tmp_path: Path) -> Path:
    """A second, different PDF — needed to prove dedup is about content."""
    return _write_pdf(
        tmp_path / "other.pdf",
        "Another Tiny Paper About Attention",
        ["1 Introduction\nA different paper on the same subject."],
    )


# ---------------------------------------------------------------------------
# stub embedder
# ---------------------------------------------------------------------------
class StubEmbedder:
    """Deterministic vectors, no torch. Same text always yields the same vector."""

    model_name = "test/embedder"
    dim = EMBEDDING_DIM

    def encode(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = (digest * ((self.dim // len(digest)) + 1))[: self.dim]
        return [(byte / 255.0) - 0.5 for byte in raw]


@pytest.fixture(autouse=True)
def stub_embedder() -> Iterator[StubEmbedder]:
    from mouseion.services import embeddings

    embedder = StubEmbedder()
    embeddings.set_embedder(embedder)  # type: ignore[arg-type]
    yield embedder
    embeddings.set_embedder(None)


# ---------------------------------------------------------------------------
# stub LLM
# ---------------------------------------------------------------------------
def make_llm_client(responses: list[dict[str, Any] | str], recorder: list[dict] | None = None):
    """An LLMClient whose transport replays canned assistant messages."""
    import json as json_module

    from mouseion.config import get_settings
    from mouseion.services.llm import LLMClient

    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.append(json_module.loads(request.content))
        payload = queue.pop(0) if queue else {}
        content = payload if isinstance(payload, str) else json_module.dumps(payload)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://openrouter.test/api/v1"
    )
    return LLMClient(get_settings(), client=client)


def extraction_payload(
    *,
    topics: list[dict[str, Any]],
    title: str | None = None,
    authors: list[str] | None = None,
    year: int | None = None,
    venue: str | None = None,
    abstract: str | None = None,
    summary_short: str = "It computes attention over inputs.",
    summary_long: str = (
        "The paper introduces a small attention mechanism, argues it matters because it "
        "replaces recurrence, and discusses toy experiments that support the claim."
    ),
) -> dict[str, Any]:
    """A valid IngestExtraction body, so tests only state what they care about."""
    return {
        "metadata": {
            "title": title,
            "authors": authors,
            "year": year,
            "venue": venue,
            "abstract": abstract,
        },
        "topics": topics,
        "summary_short": summary_short,
        "summary_long": summary_long,
    }
