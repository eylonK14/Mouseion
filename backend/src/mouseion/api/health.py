"""Public liveness plus authenticated full startup diagnostics."""

from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mouseion import __version__
from mouseion.api.models import HealthOut
from mouseion.config import get_settings
from mouseion.db import get_db, load_sqlite_vec, session
from mouseion.services.health import run_full_health
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


class FullHealthCheckOut(BaseModel):
    name: str
    status: Literal["ok", "warning", "error"]
    detail: str
    latency_ms: int


class FullHealthOut(BaseModel):
    status: Literal["ok", "degraded", "error"]
    checks: list[FullHealthCheckOut]


@router.get("/health/full", response_model=FullHealthOut)
async def full_health(conn: sqlite3.Connection = Depends(get_db)) -> FullHealthOut:
    report = await run_full_health(conn=conn)
    return FullHealthOut(
        status=report.status,
        checks=[
            FullHealthCheckOut(
                name=check.name,
                status=check.status,
                detail=check.detail,
                latency_ms=check.latency_ms,
            )
            for check in report.checks
        ],
    )
