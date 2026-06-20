"""
soy.auth
========

FastAPI authentication dependency for the Soy API.

When ``SOY_API_KEY`` is set in the environment, all ``/api/v1/*``
requests must carry an ``Authorization: Bearer <key>`` header that
matches.  When ``SOY_API_KEY`` is empty (the default), the
dependency is a no-op so existing deployments behind the nginx
basic-auth proxy continue working unchanged.

The wildcard ``*`` subscription endpoint on the WebSocket router
has its own independent HMAC-based auth (``SOY_WS_ADMIN_TOKEN``)
and is not affected by this dependency.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request, status

logger = logging.getLogger("soy.auth")

# Paths that are exempt from API key authentication even when
# ``SOY_API_KEY`` is configured.  These are either infrastructure
# endpoints (health, docs) or have their own auth (websocket).
_EXEMPT_PATH_PREFIXES = (
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/ws/",
    "/",
)


def get_api_key() -> str:
    """Read ``SOY_API_KEY`` from the environment at call time.

    The value is never cached at import time so tests that
    ``monkeypatch.setenv`` between cases are respected.
    """
    return os.getenv("SOY_API_KEY", "").strip()


async def verify_api_key(request: Request) -> None:
    """FastAPI dependency: validate the ``Authorization`` header.

    Raises HTTP 401 when the key is configured and the request
    does not carry a matching ``Bearer`` token.  Returns ``None``
    (allowing the request through) when:

    * ``SOY_API_KEY`` is empty (auth disabled), OR
    * the request path is in the exempt list, OR
    * the header matches.
    """
    api_key = get_api_key()
    if not api_key:
        # Auth not configured — open access (backward compat).
        return

    path = request.url.path
    for prefix in _EXEMPT_PATH_PREFIXES:
        # "/" is the root; match exactly "/" or paths that don't
        # start with "/api/" (the non-api surface is public).
        if prefix == "/":
            if path == "/" or not path.startswith("/api/"):
                return
        elif path.startswith(prefix):
            return

    # Require the header.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "MISSING_API_KEY", "detail": "Missing Authorization header"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    provided = auth_header[7:]  # strip "Bearer "
    if not hmac.compare_digest(
        provided.encode("utf-8"), api_key.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_API_KEY", "detail": "Invalid API key"},
            headers={"WWW-Authenticate": "Bearer"},
        )
