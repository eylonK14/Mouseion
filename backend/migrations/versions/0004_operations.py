"""Add Phase 5 pairing and request-correlation state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-13
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ingest_jobs ADD COLUMN request_id TEXT")
    op.execute("ALTER TABLE qa_log ADD COLUMN request_id TEXT")
    op.execute("CREATE INDEX ix_ingest_jobs_request_id ON ingest_jobs (request_id)")
    op.execute("CREATE INDEX ix_qa_log_request_id ON qa_log (request_id)")
    op.execute(
        """
        CREATE TABLE pairing_tokens (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at    TEXT
        )
        """
    )
    op.execute("CREATE INDEX ix_pairing_tokens_expires_at ON pairing_tokens (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS pairing_tokens")
    op.execute("DROP INDEX IF EXISTS ix_qa_log_request_id")
    op.execute("DROP INDEX IF EXISTS ix_ingest_jobs_request_id")
    op.drop_column("qa_log", "request_id")
    op.drop_column("ingest_jobs", "request_id")
