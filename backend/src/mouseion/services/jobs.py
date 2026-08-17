"""Ingest job rows and the state machine.

CLAUDE.md: ingest is asynchronous — the API returns a job id immediately and a
worker moves the job through the states below.

    queued → downloading → extracting → indexing → tagging → embedding → done
                                                                       ↘ failed

`progress_json` records which steps finished and what they produced, which is
what lets a failed job resume from the last completed step instead of starting
over (and re-paying for the LLM call).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from enum import Enum
from typing import Any


class JobState(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    INDEXING = "indexing"
    TAGGING = "tagging"
    EMBEDDING = "embedding"
    DONE = "done"
    FAILED = "failed"


class JobKind(str, Enum):
    UPLOAD = "upload"
    URL = "url"


# Pipeline steps, in order. Distinct from JobState: a state is "what is
# happening now", a step is "what has already been completed".
class Step(str, Enum):
    DOWNLOAD = "download"
    EXTRACT = "extract"
    INDEX = "index"
    TAG = "tag"
    EMBED = "embed"


TERMINAL_STATES = frozenset({JobState.DONE.value, JobState.FAILED.value})

_TOUCH = "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def create_job(conn: sqlite3.Connection, kind: JobKind | str, payload: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO ingest_jobs (id, kind, payload_json) VALUES (?, ?, ?)",
        (job_id, JobKind(kind).value, json.dumps(payload)),
    )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ingest_jobs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def set_state(
    conn: sqlite3.Connection,
    job_id: str,
    state: JobState | str,
    *,
    error: str | None = None,
) -> None:
    value = JobState(state).value
    # Clear a stale error whenever the job moves on: a resumed job that
    # succeeds must not keep advertising the failure it recovered from.
    conn.execute(
        f"UPDATE ingest_jobs SET state = ?, error = ?, {_TOUCH} WHERE id = ?",
        (value, error, job_id),
    )


def attach(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    paper_id: int | None = None,
    sha256: str | None = None,
    duplicate: bool | None = None,
) -> None:
    updates: list[str] = []
    values: list[Any] = []
    if paper_id is not None:
        updates.append("paper_id = ?")
        values.append(paper_id)
    if sha256 is not None:
        updates.append("sha256 = ?")
        values.append(sha256)
    if duplicate is not None:
        updates.append("duplicate = ?")
        values.append(int(duplicate))
    if not updates:
        return
    values.append(job_id)
    conn.execute(
        f"UPDATE ingest_jobs SET {', '.join(updates)}, {_TOUCH} WHERE id = ?", values
    )


def bump_attempts(conn: sqlite3.Connection, job_id: str) -> int:
    conn.execute(
        f"UPDATE ingest_jobs SET attempts = attempts + 1, {_TOUCH} WHERE id = ?", (job_id,)
    )
    row = conn.execute("SELECT attempts FROM ingest_jobs WHERE id = ?", (job_id,)).fetchone()
    return int(row["attempts"]) if row else 0


def get_progress(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT progress_json FROM ingest_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        return {"steps": [], "artifacts": {}}
    try:
        data = json.loads(row["progress_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    data.setdefault("steps", [])
    data.setdefault("artifacts", {})
    return data


def payload_of(row: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        return {}


def is_step_done(conn: sqlite3.Connection, job_id: str, step: Step | str) -> bool:
    return Step(step).value in get_progress(conn, job_id)["steps"]


def mark_step_done(
    conn: sqlite3.Connection, job_id: str, step: Step | str, **artifacts: Any
) -> dict[str, Any]:
    progress = get_progress(conn, job_id)
    name = Step(step).value
    if name not in progress["steps"]:
        progress["steps"].append(name)
    progress["artifacts"].update({k: v for k, v in artifacts.items() if v is not None})
    conn.execute(
        f"UPDATE ingest_jobs SET progress_json = ?, {_TOUCH} WHERE id = ?",
        (json.dumps(progress), job_id),
    )
    return progress


def reset_progress(conn: sqlite3.Connection, job_id: str) -> None:
    """Force a full re-run (every step is idempotent, so this is always safe)."""
    conn.execute(
        f"UPDATE ingest_jobs SET progress_json = '{{}}', {_TOUCH} WHERE id = ?", (job_id,)
    )
