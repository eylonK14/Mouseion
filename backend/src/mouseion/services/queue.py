"""Enqueueing side of the task queue.

Kept separate from `worker.py` so the API process imports the client only and
never pulls in worker startup code.
"""

from __future__ import annotations

import logging

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from mouseion.config import get_settings

log = logging.getLogger(__name__)

INGEST_TASK = "ingest_task"

_pool: ArqRedis | None = None


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings())
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


class EnqueueError(RuntimeError):
    """arq refused the job — it is already queued, running, or too recent."""


async def enqueue_ingest(job_id: str, *, attempt: int = 0) -> None:
    """Hand a job to the worker.

    `_job_id` deduplicates double-submits of the *same attempt* — arq drops a
    job whose id it has seen while the previous result is still retained
    (`keep_result`). The attempt number is therefore part of the id, or a retry
    of a failed job would be silently discarded for an hour instead of running.

    A refusal returns None from arq rather than raising, so it is converted into
    an exception here: an enqueue that vanishes must never look like success.
    """
    pool = await get_pool()
    job = await pool.enqueue_job(INGEST_TASK, job_id, _job_id=f"ingest:{job_id}:{attempt}")
    if job is None:
        raise EnqueueError(
            f"queue refused job {job_id} (attempt {attempt}); it is probably already running"
        )
