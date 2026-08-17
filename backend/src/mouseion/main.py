"""FastAPI application."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mouseion import __version__
from mouseion.api import health, jobs, papers
from mouseion.auth import BearerAuthMiddleware
from mouseion.config import get_settings
from mouseion.services import queue
from mouseion.services.llm import get_llm_client

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    settings.ensure_dirs()
    if not settings.api_token:
        log.warning("API_TOKEN is not set — every /api route will return 503")
    yield
    await queue.close_pool()
    await get_llm_client().aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Mouseion",
        version=__version__,
        summary="Self-hosted smart paper library",
        lifespan=lifespan,
    )

    app.add_middleware(BearerAuthMiddleware)

    app.include_router(health.router)
    app.include_router(papers.router)
    app.include_router(jobs.router)

    # Mounted last so it only catches paths no API route claimed. Phase 2
    # replaces the contents of this directory with a real build.
    frontend_dir = settings.frontend_dir
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    else:
        log.warning("FRONTEND_DIR %s does not exist; list view not served", frontend_dir)

    return app


app = create_app()
