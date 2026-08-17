"""SQLite access.

Deliberately no ORM: the schema in CLAUDE.md is small, fixed, and includes two
virtual tables (FTS5, vec0) that an ORM only gets in the way of. Alembic is used
purely as a migration runner over raw DDL.

Concurrency: API and worker are separate processes on one file, so WAL plus a
busy timeout is what keeps them off each other's toes.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from mouseion.config import get_settings

log = logging.getLogger(__name__)

_VEC_WARNED = False


def serialize_f32(vector: Sequence[float]) -> bytes:
    """Pack a float vector into the raw little-endian blob vec0 expects."""
    return struct.pack(f"<{len(vector)}f", *vector)


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension. Returns False (with one warning) if the
    interpreter or platform cannot load extensions.

    Callers that need vector features must check the result — everything else in
    Phase 1 keeps working without it, which is what lets the unit tests run on a
    stock interpreter.
    """
    global _VEC_WARNED
    try:
        import sqlite_vec  # imported lazily: optional at read time

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception as exc:  # noqa: BLE001 - any failure means "no vectors"
        if not _VEC_WARNED:
            log.warning("sqlite-vec unavailable, vector features disabled: %s", exc)
            _VEC_WARNED = True
        return False
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:  # noqa: BLE001
            pass


def connect(db_path: Path | str | None = None, *, load_vec: bool = True) -> sqlite3.Connection:
    settings = get_settings()
    path = Path(db_path) if db_path is not None else settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False because FastAPI resolves the sync `get_db`
    # dependency in a threadpool while an `async def` handler then uses the
    # connection on the event loop thread. That is safe here: a connection is
    # owned by exactly one request (or one worker job) and is never shared
    # between concurrent tasks.
    conn = sqlite3.connect(
        str(path), timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    if load_vec:
        load_sqlite_vec(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (`isolation_level=None` means autocommit otherwise).

    Used by the parts of ingest that must be all-or-nothing — notably applying a
    batch of topic proposals (CLAUDE.md: "Apply topic proposals
    transactionally").
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def session(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
