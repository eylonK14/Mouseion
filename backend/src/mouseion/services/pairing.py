"""Short-lived, single-use credentials for pairing a browser without typing."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mouseion.db import transaction


class PairingTokenError(ValueError):
    """The presented token is unknown, expired, or already consumed."""


@dataclass(frozen=True, slots=True)
class PairingToken:
    token: str
    expires_at: str


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_pairing_token(
    conn: sqlite3.Connection,
    *,
    ttl_minutes: int,
    now: datetime | None = None,
) -> PairingToken:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    expires_at = _timestamp(current + timedelta(minutes=ttl_minutes))
    token = secrets.token_urlsafe(32)
    # Remove old rows opportunistically. They are credentials, not an audit log.
    conn.execute("DELETE FROM pairing_tokens WHERE expires_at <= ?", (_timestamp(current),))
    conn.execute(
        "INSERT INTO pairing_tokens (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (_digest(token), _timestamp(current), expires_at),
    )
    return PairingToken(token=token, expires_at=expires_at)


def consume_pairing_token(
    conn: sqlite3.Connection,
    token: str,
    *,
    now: datetime | None = None,
) -> None:
    """Atomically consume ``token`` or raise a deliberately generic error."""
    candidate = token.strip()
    if not candidate:
        raise PairingTokenError("pairing link is invalid, expired, or already used")
    used_at = _timestamp((now or datetime.now(UTC)).astimezone(UTC))
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE pairing_tokens
            SET used_at = ?
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
            """,
            (used_at, _digest(candidate), used_at),
        )
        if cursor.rowcount != 1:
            raise PairingTokenError("pairing link is invalid, expired, or already used")
