"""Read-only operational summaries for the authenticated admin page."""

from __future__ import annotations

import sqlite3


def queue_counts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT state, COUNT(*) AS count FROM ingest_jobs GROUP BY state ORDER BY state"
    ).fetchall()


def failed_jobs(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, kind, state, error, paper_id, attempts, request_id, created_at, updated_at
        FROM ingest_jobs
        WHERE state = 'failed'
        ORDER BY updated_at DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def recent_jobs(conn: sqlite3.Connection, *, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, kind, state, error, paper_id, attempts, request_id, created_at, updated_at
        FROM ingest_jobs
        ORDER BY updated_at DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def qa_costs(conn: sqlite3.Connection, *, days: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, model,
               COUNT(*) AS requests,
               SUM(prompt_tokens) AS prompt_tokens,
               SUM(completion_tokens) AS completion_tokens,
               SUM(total_tokens) AS total_tokens
        FROM qa_log
        WHERE created_at >= datetime('now', ?)
        GROUP BY substr(created_at, 1, 10), model
        ORDER BY day DESC, model
        """,
        (f"-{days} days",),
    ).fetchall()
