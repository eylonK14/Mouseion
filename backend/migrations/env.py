"""Alembic environment.

The sqlite-vec extension is loaded on every connection because migration 0001
creates a `vec0` virtual table — without the extension that DDL is a syntax
error.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # allows `alembic upgrade head` without installing
    sys.path.insert(0, str(_SRC))

from mouseion.config import get_settings  # noqa: E402
from mouseion.db import load_sqlite_vec  # noqa: E402

# No ORM metadata: every migration is raw DDL, so autogenerate is intentionally
# not wired up.
target_metadata = None


def _database_url() -> str:
    settings = get_settings()
    settings.ensure_dirs()
    return f"sqlite+pysqlite:///{settings.db_path}"


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), future=True)

    @event.listens_for(engine, "connect")
    def _load_extensions(dbapi_connection, _record) -> None:  # noqa: ANN001
        load_sqlite_vec(dbapi_connection)

    # engine.begin() (not .connect()) so the connection commits on a clean
    # exit. Without it, SQLite's classic pysqlite quirk — DDL statements
    # auto-commit on their own — makes CREATE TABLE calls land even though the
    # alembic_version bookkeeping row (plain DML) never gets flushed, leaving
    # the schema fully migrated but alembic convinced it still needs to run.
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
