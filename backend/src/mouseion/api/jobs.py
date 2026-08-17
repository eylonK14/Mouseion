"""Job status — how the UI watches an ingest happen."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status

from mouseion.api.models import JobOut
from mouseion.db import get_db
from mouseion.services import queue
from mouseion.services.jobs import JobState, get_job, get_progress, set_state

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=JobOut)
def read_job(job_id: str, conn: sqlite3.Connection = Depends(get_db)) -> JobOut:
    row = get_job(conn, job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    return JobOut.from_row(row, get_progress(conn, job_id))


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str, conn: sqlite3.Connection = Depends(get_db)) -> JobOut:
    """Re-queue a failed job.

    The pipeline resumes from the last completed step (recorded in
    `progress_json`), so a job that died at `embed` does not repeat download,
    extraction, tree building, or the tagging LLM call.
    """
    row = get_job(conn, job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
    if row["state"] != JobState.FAILED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"job {job_id} is {row['state']}, only failed jobs can be retried",
        )

    set_state(conn, job_id, JobState.QUEUED)
    try:
        # The attempt number keeps this distinct from the arq job id the failed
        # run used, which is still held in Redis by keep_result.
        await queue.enqueue_ingest(job_id, attempt=int(row["attempts"]))
    except Exception as exc:  # noqa: BLE001
        set_state(conn, job_id, JobState.FAILED, error=f"could not enqueue: {exc}")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "ingest queue is unavailable"
        ) from exc

    refreshed = get_job(conn, job_id)
    assert refreshed is not None
    return JobOut.from_row(refreshed, get_progress(conn, job_id))
