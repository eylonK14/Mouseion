"""JSON logging and request-id context shared by the API and worker."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_request_id: ContextVar[str] = ContextVar("mouseion_request_id", default="")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_EXTRA_FIELDS = (
    "event",
    "method",
    "path",
    "status",
    "duration_ms",
    "job_id",
    "paper_id",
    "model",
)


def current_request_id() -> str | None:
    return _request_id.get() or None


def set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := current_request_id():
            payload["request_id"] = request_id
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "arq"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    # RequestIdMiddleware emits the access event without query strings, which
    # keeps one-time pairing credentials out of logs.
    logging.getLogger("uvicorn.access").disabled = True


class RequestIdMiddleware:
    """Correlate an HTTP request with async work and return the id to clients."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = logging.getLogger("mouseion.http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore")
        request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else uuid.uuid4().hex
        token = set_request_id(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            duration = max(0, round((time.perf_counter() - started) * 1000))
            self.log.info(
                "request complete",
                extra={
                    "event": "http_request",
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "status": status_code,
                    "duration_ms": duration,
                },
            )
            reset_request_id(token)
