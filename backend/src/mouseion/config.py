"""Configuration. Environment only (CLAUDE.md convention) — no config files.

`get_settings()` is cached, so the process reads the environment once. Tests
override by setting env vars and calling `get_settings.cache_clear()` (see
tests/conftest.py).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

TreeIndexerChoice = Literal["auto", "pageindex", "heuristic"]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:  # fail loudly: a typo'd limit is a silent bug
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


class Settings(BaseModel):
    """Typed view of the environment. Mirrors .env.example one-for-one."""

    # --- auth ---
    api_token: str = ""

    # --- LLM routing (OpenRouter) ---
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_ingest: str = "anthropic/claude-haiku-4.5"
    model_qa: str = "anthropic/claude-opus-4.5"
    openrouter_app_url: str = "http://localhost:8000"
    openrouter_app_title: str = "Mouseion"
    llm_timeout_seconds: int = 180
    llm_max_attempts: int = 2

    # --- embeddings (local) ---
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # --- tree index ---
    tree_indexer: TreeIndexerChoice = "auto"
    pageindex_mode: str = "flash"

    # --- storage ---
    data_dir: Path = Field(default=Path("./data"))
    db_path: Path = Field(default=Path("./data/library.db"))

    # --- queue ---
    redis_url: str = "redis://localhost:6379/0"

    # --- ingest limits ---
    max_upload_mb: int = 64
    http_timeout_seconds: int = 60
    arxiv_api_base: str = "http://export.arxiv.org/api/query"
    ingest_text_budget_chars: int = 24_000

    # --- app ---
    log_level: str = "INFO"
    frontend_dir: Path = Field(default=Path("./frontend"))

    # Where Open WebUI is reachable *from the browser*, not from inside the
    # compose network. Phase 2 only needs it to build the (disabled) "Ask about
    # this paper" / "Test me" deep links; Phase 3 turns them on, and the link
    # target is already decided: a chat pre-filled with `[paper:{id}] ...`.
    openwebui_base_url: str = "http://localhost:3000"

    @property
    def templates_dir(self) -> Path:
        """Jinja templates for the server-rendered UI."""
        return self.frontend_dir / "templates"

    @property
    def static_dir(self) -> Path:
        """CSS/JS, served publicly at /static (see auth.py)."""
        return self.frontend_dir / "static"

    @property
    def pdf_dir(self) -> Path:
        """Content-addressed PDF store: data/pdfs/<sha256>.pdf."""
        return self.data_dir / "pdfs"

    @property
    def tree_dir(self) -> Path:
        """PageIndex trees: data/trees/<sha256>.json."""
        return self.data_dir / "trees"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.pdf_dir, self.tree_dir, self.db_path.parent):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "./data"))
    return Settings(
        api_token=_env("API_TOKEN"),
        openrouter_api_key=_env("OPENROUTER_API_KEY"),
        openrouter_base_url=_env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        model_ingest=_env("MODEL_INGEST", "anthropic/claude-haiku-4.5"),
        model_qa=_env("MODEL_QA", "anthropic/claude-opus-4.5"),
        openrouter_app_url=_env("OPENROUTER_APP_URL", "http://localhost:8000"),
        openrouter_app_title=_env("OPENROUTER_APP_TITLE", "Mouseion"),
        llm_timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 180),
        llm_max_attempts=_env_int("LLM_MAX_ATTEMPTS", 2),
        embedding_model=_env("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        embedding_dim=_env_int("EMBEDDING_DIM", 384),
        tree_indexer=_env("TREE_INDEXER", "auto"),  # type: ignore[arg-type]
        pageindex_mode=_env("PAGEINDEX_MODE", "flash"),
        data_dir=data_dir,
        db_path=Path(_env("DB_PATH", str(data_dir / "library.db"))),
        redis_url=_env("REDIS_URL", "redis://localhost:6379/0"),
        max_upload_mb=_env_int("MAX_UPLOAD_MB", 64),
        http_timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", 60),
        arxiv_api_base=_env("ARXIV_API_BASE", "http://export.arxiv.org/api/query"),
        ingest_text_budget_chars=_env_int("INGEST_TEXT_BUDGET_CHARS", 24_000),
        log_level=_env("LOG_LEVEL", "INFO"),
        frontend_dir=Path(_env("FRONTEND_DIR", "./frontend")),
        openwebui_base_url=_env("OPENWEBUI_BASE_URL", "http://localhost:3000").rstrip("/"),
    )
