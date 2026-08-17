"""arq worker.

Why arq rather than Celery/RQ/dramatiq (the justification the build asked for):

* The pipeline is I/O bound and already async — arXiv lookups, PDF downloads,
  OpenRouter calls. arq is asyncio-native, so the worker runs *the same*
  `run_ingest` coroutine the tests call directly, sharing one httpx client and
  one event loop. Celery is sync-first and would need an async bridge at every
  one of those call sites.
* It needs only Redis, which the stack can afford; no result backend, no
  separate beat process, no broker configuration language.
* It is small enough to reason about: one `WorkerSettings` class, and a job is
  a plain coroutine function.

The cost is a Redis dependency for what is a single-user app. That is accepted
because durability across container restarts is worth more than one small
container — an in-process asyncio.Queue would lose queued work on every deploy.

Retries: `max_tries = 1`. Retry semantics live in the job row instead
(`progress_json` + POST /api/jobs/{id}/retry), so a resumed job skips the steps
that already succeeded rather than repeating the expensive LLM call.
"""

from __future__ import annotations

import logging
from typing import Any

from mouseion.config import get_settings
from mouseion.services.embeddings import get_embedder
from mouseion.services.ingest import run_ingest
from mouseion.services.llm import get_llm_client
from mouseion.services.queue import INGEST_TASK, redis_settings

log = logging.getLogger(__name__)


async def ingest_task(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    outcome = await run_ingest(job_id)
    return {
        "job_id": outcome.job_id,
        "paper_id": outcome.paper_id,
        "duplicate": outcome.duplicate,
        "warnings": outcome.warnings,
    }


ingest_task.__name__ = INGEST_TASK


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    settings.ensure_dirs()

    # Load the embedding model once per worker process, not once per job.
    # Failure here is not fatal: the first job will try again and report a real
    # error if the model genuinely cannot be loaded.
    try:
        get_embedder().encode("warmup")
        log.info("embedding model ready: %s", settings.embedding_model)
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding model not preloaded: %s", exc)


async def shutdown(ctx: dict[str, Any]) -> None:
    await get_llm_client().aclose()


class WorkerSettings:
    functions = [ingest_task]
    on_startup = startup
    on_shutdown = shutdown
    max_tries = 1
    job_timeout = 900  # a long PDF plus a slow model
    keep_result = 3600
    # arq reads this as a plain RedisSettings instance (not a callable), so it
    # must be evaluated at class-definition time rather than defined as a
    # method — a @staticmethod here makes arq hand the descriptor itself to
    # `settings.host`, which is what was crash-looping the container.
    redis_settings = redis_settings()
