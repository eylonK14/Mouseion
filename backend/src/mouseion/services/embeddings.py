"""Local embeddings (sentence-transformers) into sqlite-vec.

CLAUDE.md: embeddings are LOCAL, one vector per paper, over
`title + abstract + topic names + summary_long`. No embedding API calls.

Phase 1 generates them at ingest even though retrieval is a Phase 3 concern, so
Phase 3 opens onto a populated index instead of a backfill job.

The model is loaded once per process (`get_embedder` is cached) — reloading a
transformer per job would dominate ingest time.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from mouseion.config import get_settings
from mouseion.db import load_sqlite_vec, serialize_f32

log = logging.getLogger(__name__)


class Embedder:
    """Thin wrapper over a SentenceTransformer. Imported lazily so that neither
    the API process nor the unit tests pay for torch."""

    def __init__(self, model_name: str, dim: int) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model = None

    def _load(self):  # noqa: ANN202 - SentenceTransformer, imported lazily
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            log.info("loading embedding model %s", self.model_name)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def encode(self, text: str) -> list[float]:
        vector = self._load().encode(text, normalize_embeddings=True)
        return [float(x) for x in vector]


_override: Embedder | None = None


def set_embedder(embedder: Embedder | None) -> None:
    """Test seam: install a stub so the suite never downloads a model."""
    global _override
    _override = embedder
    get_embedder.cache_clear()


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    if _override is not None:
        return _override
    settings = get_settings()
    return Embedder(settings.embedding_model, settings.embedding_dim)


@dataclass(frozen=True, slots=True)
class EmbeddingSource:
    text: str

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def build_embedding_source(
    *,
    title: str | None,
    abstract: str | None,
    topic_names: Sequence[str] = (),
    summary_long: str | None = None,
) -> EmbeddingSource:
    """Exactly the four fields CLAUDE.md names, in a stable order."""
    parts = [
        (title or "").strip(),
        (abstract or "").strip(),
        ", ".join(name.strip() for name in topic_names if name.strip()),
        (summary_long or "").strip(),
    ]
    return EmbeddingSource("\n\n".join(part for part in parts if part))


def store_embedding(
    conn: sqlite3.Connection,
    paper_id: int,
    vector: Sequence[float],
    *,
    model: str,
    source: str = "title+abstract+topics+summary_long",
) -> bool:
    """Upsert the paper's vector. Returns False if sqlite-vec is unavailable.

    The model name goes into `paper_embeddings` alongside it, which is what
    makes a later re-embedding traceable: Phase 3 can select every paper whose
    recorded model differs from the configured one.
    """
    if not load_sqlite_vec(conn):
        log.warning("skipping embedding for paper %s: sqlite-vec unavailable", paper_id)
        return False

    dim = len(vector)
    blob = serialize_f32(vector)
    # vec0 has no UPSERT; delete-then-insert makes re-running the step safe.
    conn.execute("DELETE FROM paper_vectors WHERE paper_id = ?", (paper_id,))
    conn.execute(
        "INSERT INTO paper_vectors (paper_id, embedding) VALUES (?, ?)",
        (paper_id, blob),
    )
    conn.execute(
        """
        INSERT INTO paper_embeddings (paper_id, model, dim, source)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (paper_id) DO UPDATE SET
            model = excluded.model,
            dim = excluded.dim,
            source = excluded.source,
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (paper_id, model, dim, source),
    )
    return True


def embedding_info(conn: sqlite3.Connection, paper_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT paper_id, model, dim, source, updated_at FROM paper_embeddings WHERE paper_id = ?",
        (paper_id,),
    ).fetchone()


def papers_with_stale_embeddings(conn: sqlite3.Connection, model: str) -> list[int]:
    """Phase 3+ helper: papers embedded with some other model (or not at all)."""
    rows = conn.execute(
        """
        SELECT p.id
        FROM papers p
        LEFT JOIN paper_embeddings e ON e.paper_id = p.id
        WHERE e.paper_id IS NULL OR e.model <> ?
        ORDER BY p.id
        """,
        (model,),
    ).fetchall()
    return [row["id"] for row in rows]
