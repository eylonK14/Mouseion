"""Full startup diagnostics shared by HTTP, admin, and restore drills."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import time
from dataclasses import dataclass
from typing import Literal

from mouseion.config import Settings, get_settings
from mouseion.db import session
from mouseion.services.embeddings import Embedder, get_embedder
from mouseion.services.llm import LLMClient, get_llm_client
from mouseion.services.tree_indexer import pageindex_available

CheckStatus = Literal["ok", "warning", "error"]


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    status: CheckStatus
    detail: str
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: Literal["ok", "degraded", "error"]
    checks: list[HealthCheck]


def _result(name: str, started: float, status: CheckStatus, detail: str) -> HealthCheck:
    return HealthCheck(name, status, detail, max(0, round((time.perf_counter() - started) * 1000)))


def _check_database(conn: sqlite3.Connection) -> HealthCheck:
    started = time.perf_counter()
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            return _result("database", started, "error", f"SQLite quick_check: {quick}")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS __mouseion_health_probe (value INTEGER)")
            conn.execute("INSERT INTO __mouseion_health_probe (value) VALUES (1)")
        finally:
            conn.execute("ROLLBACK")
        return _result("database", started, "ok", "SQLite is writable; quick_check passed")
    except Exception as exc:  # noqa: BLE001
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return _result("database", started, "error", f"{type(exc).__name__}: {exc}")


def _check_fts(conn: sqlite3.Connection) -> HealthCheck:
    started = time.perf_counter()
    try:
        paper_count = int(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        fts_count = int(conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0])
        missing = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM papers p
                LEFT JOIN papers_fts f ON f.rowid = p.id
                WHERE f.rowid IS NULL
                """
            ).fetchone()[0]
        )
        if paper_count != fts_count or missing:
            return _result(
                "fts",
                started,
                "error",
                f"out of sync: papers={paper_count}, fts={fts_count}, missing={missing}",
            )
        return _result("fts", started, "ok", f"{paper_count} paper row(s) indexed")
    except Exception as exc:  # noqa: BLE001
        return _result("fts", started, "error", f"{type(exc).__name__}: {exc}")


async def _check_embedding(settings: Settings, embedder: Embedder) -> HealthCheck:
    started = time.perf_counter()
    try:
        vector = await asyncio.to_thread(embedder.encode, "Mouseion embedding health check")
        if len(vector) != settings.embedding_dim:
            return _result(
                "embedding",
                started,
                "error",
                f"model returned {len(vector)} dimensions; schema expects {settings.embedding_dim}",
            )
        return _result("embedding", started, "ok", f"{embedder.model_name} loaded ({len(vector)}d)")
    except Exception as exc:  # noqa: BLE001
        return _result("embedding", started, "error", f"{type(exc).__name__}: {exc}")


async def _check_openrouter(settings: Settings, llm: LLMClient) -> HealthCheck:
    started = time.perf_counter()
    if not settings.openrouter_api_key:
        return _result("openrouter", started, "error", "OPENROUTER_API_KEY is not configured")
    try:
        await llm.check_reachable(timeout_seconds=settings.health_openrouter_timeout_seconds)
        return _result("openrouter", started, "ok", "OpenRouter models endpoint is reachable")
    except Exception as exc:  # noqa: BLE001
        return _result("openrouter", started, "error", f"{type(exc).__name__}: {exc}")


def _check_pageindex(settings: Settings) -> HealthCheck:
    started = time.perf_counter()
    available = pageindex_available()
    if available:
        return _result("pageindex", started, "ok", "PageIndex import succeeded")
    if settings.tree_indexer == "heuristic":
        return _result(
            "pageindex", started, "ok", "heuristic indexer selected; PageIndex not required"
        )
    status: CheckStatus = "error" if settings.tree_indexer == "pageindex" else "warning"
    return _result(
        "pageindex",
        started,
        status,
        "PageIndex is unavailable; auto mode uses the heuristic fallback",
    )


def _check_disk(settings: Settings) -> HealthCheck:
    started = time.perf_counter()
    try:
        free = shutil.disk_usage(settings.data_dir).free
        free_mb = free // (1024 * 1024)
        status: CheckStatus = "ok" if free_mb >= settings.health_min_free_disk_mb else "error"
        return _result(
            "disk",
            started,
            status,
            f"{free_mb:,} MiB free; minimum is {settings.health_min_free_disk_mb:,} MiB",
        )
    except Exception as exc:  # noqa: BLE001
        return _result("disk", started, "error", f"{type(exc).__name__}: {exc}")


async def run_full_health(
    *,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    llm: LLMClient | None = None,
    check_external: bool = True,
) -> HealthReport:
    settings = settings or get_settings()

    async def collect(connection: sqlite3.Connection) -> list[HealthCheck]:
        checks = [_check_database(connection), _check_fts(connection)]
        checks.append(await _check_embedding(settings, embedder or get_embedder()))
        checks.append(_check_pageindex(settings))
        checks.append(_check_disk(settings))
        if check_external:
            checks.append(await _check_openrouter(settings, llm or get_llm_client()))
        else:
            checks.append(HealthCheck("openrouter", "warning", "external check skipped", 0))
        return checks

    if conn is not None:
        checks = await collect(conn)
    else:
        with session() as owned:
            checks = await collect(owned)
    statuses = {check.status for check in checks}
    overall: Literal["ok", "degraded", "error"]
    overall = "error" if "error" in statuses else "degraded" if "warning" in statuses else "ok"
    return HealthReport(overall, checks)
