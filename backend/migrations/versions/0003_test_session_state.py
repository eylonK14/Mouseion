"""Add resumable examiner state to test sessions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-23
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE test_sessions ADD COLUMN phase TEXT NOT NULL DEFAULT 'explain' "
        "CHECK (phase IN ('explain', 'probe', 'verdict'))"
    )
    op.execute(
        "ALTER TABLE test_sessions ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0 "
        "CHECK (turn_count >= 0)"
    )
    op.execute(
        "ALTER TABLE test_sessions ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}'"
    )
    # SQLite does not allow a non-constant expression as an ALTER TABLE default.
    # New sessions always supply these timestamps; the UPDATE preserves any
    # pre-Phase-4 rows without rebuilding the table that migration 0001 owns.
    op.execute(
        "ALTER TABLE test_sessions ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
    )
    op.execute("ALTER TABLE test_sessions ADD COLUMN expires_at TEXT")
    op.execute("ALTER TABLE test_sessions ADD COLUMN completed_at TEXT")
    op.execute(
        "UPDATE test_sessions SET updated_at = created_at WHERE updated_at = ''"
    )
    op.execute(
        "CREATE INDEX ix_test_sessions_paper_created "
        "ON test_sessions (paper_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_test_sessions_active_expiry "
        "ON test_sessions (phase, expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_test_sessions_active_expiry")
    op.execute("DROP INDEX IF EXISTS ix_test_sessions_paper_created")
    op.drop_column("test_sessions", "completed_at")
    op.drop_column("test_sessions", "expires_at")
    op.drop_column("test_sessions", "updated_at")
    op.drop_column("test_sessions", "state_json")
    op.drop_column("test_sessions", "turn_count")
    op.drop_column("test_sessions", "phase")
