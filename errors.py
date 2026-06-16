"""
soy.errors
==========

Structured error helpers and FastAPI exception handlers.

The validation contract requires every error response to expose a
machine-readable ``code`` field at the top level alongside a human
``detail`` string. FastAPI's default ``HTTPException`` handler
envelopes the body in ``{"detail": ...}`` which would push our
structured body one level too deep. We solve this by:

1. Providing :func:`raise_http_error` for use in route handlers
   that builds the structured body and then raises an
   ``HTTPException`` with a *dict* ``detail``. FastAPI's default
   handler preserves the dict's shape inside the ``detail`` key —
   so a caller sees ``{"detail": {"code": ..., "detail": ...}}``.
2. Registering :func:`http_exception_handler` in
   :func:`register_exception_handlers` to flatten the envelope
   so the caller sees ``{"code": ..., "detail": ...}`` at the top
   level. When the ``detail`` is a plain string the handler falls
   back to the FastAPI default behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("soy.errors")


def _err(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    """Build a structured error body.

    Always returns a dict with at least ``code`` and ``detail`` keys.
    Extra kwargs are merged at the top level so callers can attach
    context (e.g. ``allowed=[...]``) to the body.
    """
    body: Dict[str, Any] = {"code": code, "detail": message}
    body.update(extra)
    return body


def raise_http_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    **extra: Any,
) -> None:
    """Raise an ``HTTPException`` with a structured body.

    The body is built with :func:`_err` and stored under the
    ``detail`` key (the only place FastAPI allows a dict). The
    registered :func:`http_exception_handler` unwraps it so the
    response shape is the structured ``{code, detail, ...}`` the
    contract requires.
    """
    raise HTTPException(
        status_code=status_code,
        detail=_err(code, message, **extra),
        headers=headers,
    )


async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Unwrap ``HTTPException`` bodies so ``code`` is at the top level.

    FastAPI's default behaviour is to render ``HTTPException`` as
    ``{"detail": <whatever the caller passed>}``. When the caller
    passed a dict we flatten it so the structured error body lands
    at the top level of the response.
    """
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        body = dict(exc.detail)
    else:
        # Strings (or anything without a ``code``) are passed through
        # under the default ``detail`` key — matches FastAPI's stock
        # behaviour.
        body = {"detail": exc.detail}
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Render Pydantic validation errors as a structured 422 body.

    Pydantic's default 422 body is ``{"detail": [...]}`` where the
    list contains ``{"loc": [...], "msg": ..., "type": ...}`` items.
    The contract preserves that shape (the Pydantic ``detail`` array
    *is* the structured error) and adds a top-level ``code`` of
    ``"VALIDATION_ERROR"`` so the client can branch on it.
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": "VALIDATION_ERROR",
            "detail": exc.errors(),
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the structured error handlers on ``app``.

    Called from :mod:`soy.main` so every endpoint (including future
    routers for agents, tasks, executions, approvals, chat) inherits
    the same shape.
    """
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
