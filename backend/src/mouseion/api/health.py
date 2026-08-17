"""Health check — the one route that is not behind the bearer token."""

from __future__ import annotations

from fastapi import APIRouter

from mouseion import __version__
from mouseion.api.models import HealthOut
from mouseion.config import get_settings
from mouseion.db import load_sqlite_vec, session
from mouseion.services.tree_indexer import get_tree_indexer

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    # Report which optional pieces actually came up, so a stack that started
    # but cannot embed or build trees says so instead of looking healthy.
    with session() as conn:
        has_vec = load_sqlite_vec(conn)
    return HealthOut(
        version=__version__,
        sqlite_vec=has_vec,
        tree_indexer=get_tree_indexer(get_settings()).name,
    )
