"""Bearer-token auth.

CLAUDE.md: single user, but the API requires a bearer token because it is
reachable from a phone over Tailscale.

Exemptions, and why they are the only ones:

* `/health` — required by the compose healthcheck, exposes nothing.
* The UI shells (`/`, `/papers/{id}`, `/taxonomy`, `/static/*`) — a browser
  cannot attach an `Authorization` header to a top-level navigation, so a
  protected shell would be unreachable by design. A shell is markup containing
  no library data; it prompts for the token and sends it on every `/api/*` and
  `/ui/*` request, all of which are protected. In particular `/papers/{id}`
  renders without looking the paper up, so being public does not even leak
  which ids exist. `/docs` and `/openapi.json` are NOT exempt.

Fails closed: with no API_TOKEN configured, every protected route returns 503
rather than silently accepting anonymous traffic.
"""

from __future__ import annotations

import re
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mouseion.config import get_settings

PUBLIC_PATHS = frozenset({"/health", "/", "/index.html", "/favicon.ico", "/taxonomy"})
PUBLIC_PREFIXES = ("/static/",)
# Only the shell. `/papers/12/anything` is not a route, and matching loosely
# here would expose whatever a later phase mounts underneath it.
PUBLIC_PATTERNS = (re.compile(r"^/papers/\d+$"),)


def is_public(path: str) -> bool:
    return (
        path in PUBLIC_PATHS
        or path.startswith(PUBLIC_PREFIXES)
        or any(pattern.match(path) for pattern in PUBLIC_PATTERNS)
    )


def extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class BearerAuthMiddleware:
    """Pure-ASGI so it adds no per-request task group (BaseHTTPMiddleware does)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if is_public(request.url.path):
            await self.app(scope, receive, send)
            return

        expected = get_settings().api_token
        if not expected:
            await JSONResponse(
                {"detail": "API_TOKEN is not configured on the server"}, status_code=503
            )(scope, receive, send)
            return

        presented = extract_bearer(request.headers.get("authorization"))
        if presented is None or not secrets.compare_digest(presented, expected):
            await JSONResponse(
                {"detail": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)
