"""Initial schema — the whole of CLAUDE.md's storage section.

Includes tables Phase 1 barely touches (`notes`, `test_sessions`): they are
free to create now and creating them later would mean a migration per phase
against a live database.

Revision ID: 0001
Revises:
Create Date: 2026-08-15
"""

from __future__ import annotations

from alembic import op

from mouseion.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Paper reading workflow (CLAUDE.md: status ∈ {...}, default to_read).
PAPER_STATUSES = ("to_read", "reading", "read", "understood")

# Ingest job state machine. `downloading` covers "fetch the bytes" for URL jobs
# and is skipped for uploads, which already have them.
JOB_STATES = (
    "queued",
    "downloading",
    "extracting",
    "indexing",
    "tagging",
    "embedding",
    "done",
    "failed",
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    # ---------------------------------------------------------------- papers
    op.execute(
        f"""
        CREATE TABLE papers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            sha256        TEXT    NOT NULL UNIQUE,
            title         TEXT,
            -- "; "-joined display string. Kept as one column (per CLAUDE.md)
            -- rather than a side table so the FTS triggers stay trivial;
            -- services/papers.py owns the split/join.
            authors       TEXT,
            year          INTEGER,
            venue         TEXT,
            source_url    TEXT,
            abstract      TEXT,
            summary_short TEXT,
            summary_long  TEXT,
            status        TEXT    NOT NULL DEFAULT 'to_read'
                          CHECK ({_in_list("status", PAPER_STATUSES)}),
            added_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_papers_added_at ON papers (added_at DESC)")
    op.execute("CREATE INDEX ix_papers_status ON papers (status)")
    op.execute("CREATE INDEX ix_papers_source_url ON papers (source_url)")

    # Full text lives beside `papers` rather than in it: it is megabytes per row
    # and every list query would otherwise drag it through the page cache.
    op.execute(
        """
        CREATE TABLE paper_texts (
            paper_id     INTEGER PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
            full_text    TEXT    NOT NULL DEFAULT '',
            n_pages      INTEGER,
            extracted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )

    # ---------------------------------------------------------------- topics
    # COLLATE NOCASE gives case-insensitive uniqueness at the storage layer, so
    # a model proposing "Attention Mechanisms" when "attention mechanisms"
    # already exists cannot create a twin even if the service layer is bypassed.
    op.execute(
        """
        CREATE TABLE topics (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL COLLATE NOCASE UNIQUE,
            parent_id  INTEGER REFERENCES topics (id) ON DELETE SET NULL,
            created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_topics_parent_id ON topics (parent_id)")

    op.execute(
        """
        CREATE TABLE paper_topics (
            paper_id INTEGER NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
            topic_id INTEGER NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
            PRIMARY KEY (paper_id, topic_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_paper_topics_topic_id ON paper_topics (topic_id)")

    # ----------------------------------------------------- notes / test mode
    # Unused in Phase 1. Phase 2 writes notes; Phase 4 writes test_sessions.
    op.execute(
        """
        CREATE TABLE notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id   INTEGER NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
            content    TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_notes_paper_id ON notes (paper_id)")

    op.execute(
        """
        CREATE TABLE test_sessions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id        INTEGER NOT NULL REFERENCES papers (id) ON DELETE CASCADE,
            transcript_json TEXT    NOT NULL DEFAULT '[]',
            rubric_json     TEXT    NOT NULL DEFAULT '{}',
            score           REAL,
            gaps_json       TEXT    NOT NULL DEFAULT '[]',
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_test_sessions_paper_id ON test_sessions (paper_id)")

    # ----------------------------------------------------------- ingest jobs
    # `progress_json` is what makes a failed job resumable: it records which
    # pipeline steps already completed and the artifacts they produced, so a
    # retry skips straight to the first unfinished step.
    op.execute(
        f"""
        CREATE TABLE ingest_jobs (
            id            TEXT    PRIMARY KEY,
            kind          TEXT    NOT NULL CHECK (kind IN ('upload', 'url')),
            payload_json  TEXT    NOT NULL DEFAULT '{{}}',
            state         TEXT    NOT NULL DEFAULT 'queued'
                          CHECK ({_in_list("state", JOB_STATES)}),
            error         TEXT,
            paper_id      INTEGER REFERENCES papers (id) ON DELETE SET NULL,
            sha256        TEXT,
            duplicate     INTEGER NOT NULL DEFAULT 0,
            attempts      INTEGER NOT NULL DEFAULT 0,
            progress_json TEXT    NOT NULL DEFAULT '{{}}',
            created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_ingest_jobs_state ON ingest_jobs (state)")
    op.execute("CREATE INDEX ix_ingest_jobs_created_at ON ingest_jobs (created_at DESC)")
    op.execute("CREATE INDEX ix_ingest_jobs_sha256 ON ingest_jobs (sha256)")

    # ------------------------------------------------------------------ FTS5
    # Live from day one so Phase 2 only writes queries — there is never a
    # backfill step. rowid is pinned to papers.id, which makes every trigger a
    # single-row operation.
    op.execute(
        """
        CREATE VIRTUAL TABLE papers_fts USING fts5 (
            title,
            authors,
            abstract,
            summary_short,
            summary_long,
            full_text,
            paper_id UNINDEXED,
            tokenize = 'porter unicode61'
        )
        """
    )

    fts_columns = (
        "rowid, paper_id, title, authors, abstract, summary_short, summary_long, full_text"
    )
    fts_values = """
            new.id,
            new.id,
            COALESCE(new.title, ''),
            COALESCE(new.authors, ''),
            COALESCE(new.abstract, ''),
            COALESCE(new.summary_short, ''),
            COALESCE(new.summary_long, ''),
            COALESCE((SELECT full_text FROM paper_texts WHERE paper_id = new.id), '')
    """

    op.execute(
        f"""
        CREATE TRIGGER papers_ai AFTER INSERT ON papers BEGIN
            INSERT INTO papers_fts ({fts_columns}) VALUES ({fts_values});
        END
        """
    )
    # Delete-then-insert rather than a column-wise UPDATE: it keeps this trigger
    # correct no matter which subset of columns the update touched.
    op.execute(
        f"""
        CREATE TRIGGER papers_au AFTER UPDATE ON papers BEGIN
            DELETE FROM papers_fts WHERE rowid = old.id;
            INSERT INTO papers_fts ({fts_columns}) VALUES ({fts_values});
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER papers_ad AFTER DELETE ON papers BEGIN
            DELETE FROM papers_fts WHERE rowid = old.id;
        END
        """
    )

    # Full text arrives after the paper row, so it is synced separately.
    op.execute(
        """
        CREATE TRIGGER paper_texts_ai AFTER INSERT ON paper_texts BEGIN
            UPDATE papers_fts SET full_text = new.full_text WHERE rowid = new.paper_id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER paper_texts_au AFTER UPDATE ON paper_texts BEGIN
            UPDATE papers_fts SET full_text = new.full_text WHERE rowid = new.paper_id;
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER paper_texts_ad AFTER DELETE ON paper_texts BEGIN
            UPDATE papers_fts SET full_text = '' WHERE rowid = old.paper_id;
        END
        """
    )

    # ------------------------------------------------------------- sqlite-vec
    # ONE embedding per paper, not per chunk (CLAUDE.md).
    #
    # The model name lives in the companion table rather than as a vec0 metadata
    # column: metadata-column support varies across sqlite-vec releases, and a
    # plain table is also what lets Phase 3 ask "which papers were embedded with
    # a model other than the current one?" with an ordinary query before
    # re-embedding.
    dim = get_settings().embedding_dim
    op.execute(
        f"""
        CREATE VIRTUAL TABLE paper_vectors USING vec0 (
            paper_id INTEGER PRIMARY KEY,
            embedding FLOAT[{dim}]
        )
        """
    )
    op.execute(
        """
        CREATE TABLE paper_embeddings (
            paper_id   INTEGER PRIMARY KEY REFERENCES papers (id) ON DELETE CASCADE,
            model      TEXT    NOT NULL,
            dim        INTEGER NOT NULL,
            source     TEXT    NOT NULL DEFAULT '',
            created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_paper_embeddings_model ON paper_embeddings (model)")


def downgrade() -> None:
    for trigger in (
        "paper_texts_ad",
        "paper_texts_au",
        "paper_texts_ai",
        "papers_ad",
        "papers_au",
        "papers_ai",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in (
        "paper_embeddings",
        "paper_vectors",
        "papers_fts",
        "ingest_jobs",
        "test_sessions",
        "notes",
        "paper_topics",
        "topics",
        "paper_texts",
        "papers",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table}")
