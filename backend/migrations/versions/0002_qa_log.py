"""Add per-request grounded-QA cost and grounding visibility.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qa_log (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            scope                    TEXT    NOT NULL CHECK (scope IN ('collection', 'paper')),
            paper_id                 INTEGER REFERENCES papers (id) ON DELETE SET NULL,
            topic_id                 INTEGER REFERENCES topics (id) ON DELETE SET NULL,
            model                    TEXT    NOT NULL,
            prompt_tokens            INTEGER NOT NULL DEFAULT 0,
            completion_tokens        INTEGER NOT NULL DEFAULT 0,
            total_tokens             INTEGER NOT NULL DEFAULT 0,
            latency_ms               INTEGER NOT NULL DEFAULT 0,
            candidate_count          INTEGER NOT NULL DEFAULT 0,
            navigation_count         INTEGER NOT NULL DEFAULT 0,
            consulted_papers_json    TEXT    NOT NULL DEFAULT '[]',
            citation_warnings_json   TEXT    NOT NULL DEFAULT '[]',
            error                    TEXT,
            created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    op.execute("CREATE INDEX ix_qa_log_created_at ON qa_log (created_at DESC)")
    op.execute("CREATE INDEX ix_qa_log_paper_id ON qa_log (paper_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qa_log")
