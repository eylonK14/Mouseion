"""FastAPI application."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from mouseion import __version__
from mouseion.api import health, jobs, notes, papers, qa, search, topics, ui
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
    app.include_router(notes.router)
    app.include_router(search.router)
    app.include_router(qa.router)
    app.include_router(topics.router)
    app.include_router(jobs.router)

    static_dir = settings.static_dir
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    else:
        log.warning("static dir %s does not exist; the UI will be unstyled", static_dir)

    # Last: the UI owns "/" and "/papers/{id}", which must not shadow any API
    # route. (They cannot — every API route is under /api — but the ordering
    # keeps that true if one is ever added at the root.)
    if settings.templates_dir.is_dir():
        ui.reset_templates()
        app.include_router(ui.router)
    else:
        log.warning("templates dir %s does not exist; UI not served", settings.templates_dir)

    return app


app = create_app()
