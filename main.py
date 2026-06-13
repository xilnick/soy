"""
asf.main
========

FastAPI entrypoint for the AI Software Factory.

The application is intentionally minimal at this milestone — it exposes
``/health`` so the deploy blueprint can prove the service is up, and
its ``lifespan`` hook runs ``alembic upgrade head`` on startup so the
schema is always in sync with the code, even when a fresh image is
deployed.

The schema-management concern is intentionally implemented in
:mod:`asf.db` (the ``run_alembic_upgrade`` helper) so that the same
function can be invoked from:

  * the FastAPI lifespan hook (this file);
  * the Piperoni deploy blueprint (so re-deploys also re-run
    migrations explicitly);
  * unit tests that need a freshly migrated database.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from asf.api.v1.router import api_v1_router, ws_router
from asf.db import run_alembic_upgrade
from asf.errors import register_exception_handlers
from asf.ws.events import install_as_publisher

logger = logging.getLogger("asf.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run migrations on startup, log shutdown on exit.

    The ``ASF_RUN_MIGRATIONS_ON_STARTUP`` env var is honoured so unit
    tests that mount a pre-migrated SQLite in-memory database can
    skip the Alembic invocation. The variable is read at the top of
    the function (not module import time) so test fixtures that
    toggle it are respected.
    """
    # Install structured (JSON) logging to stdout first so every
    # subsequent startup line — including migration output — is
    # captured in the structured format PM2 ships.
    from asf.logging_config import configure_logging

    configure_logging()

    run_migrations = os.environ.get(
        "ASF_RUN_MIGRATIONS_ON_STARTUP", "true"
    ).lower() in ("1", "true", "yes")
    if run_migrations:
        try:
            run_alembic_upgrade()
        except Exception:  # noqa: BLE001 — log and continue
            # We deliberately do not abort startup on migration
            # failure: the operational signal is that ``/health``
            # returns 503 and the PM2 log captures the stack trace.
            logger.exception("ASF Alembic migration failed on startup")
    else:
        logger.info(
            "ASF startup migrations skipped "
            "(ASF_RUN_MIGRATIONS_ON_STARTUP=%s)",
            os.environ.get("ASF_RUN_MIGRATIONS_ON_STARTUP"),
        )
    yield
    logger.info("ASF shutdown")
    # Remove the stdout log handler we installed so it does not outlive
    # the app (keeps the root logger clean across per-test lifespans).
    from asf.logging_config import reset_logging

    reset_logging()


app = FastAPI(
    title="ASF Backend",
    version="0.1.0",
    description=(
        "AI Software Factory — FastAPI mission orchestration backend. "
        "See ``asf/models/`` for the schema and ``asf/alembic/`` for "
        "the migrations."
    ),
    lifespan=lifespan,
)

# Structured error handlers — every error response carries a top-level
# ``code`` field alongside ``detail``. See :mod:`asf.errors`.
register_exception_handlers(app)

# API v1 router — mission CRUD + state machine.
app.include_router(api_v1_router)

# WebSocket router — real-time mission events.
app.include_router(ws_router)

# Wire the ASF worker's event publisher to the in-memory WebSocket
# bus so successful executions, retries, and escalations are
# broadcast to every connected client in real time. The publisher
# uses ``asyncio.run_coroutine_threadsafe`` internally so the
# worker can publish from any thread (the executor pool runs
# PraisonAI in background threads).
install_as_publisher()


@app.get("/health")
async def health() -> dict:
    """Liveness probe — returns 200 OK when the service is up.

    The endpoint deliberately does not check the database connection:
    it is used by PM2, the deploy blueprint, and the health timer to
    confirm the process is alive. Database connectivity is verified
    by the ``/api/v1/health/db`` endpoint (added by a later feature).
    """
    return {
        "status": "ok",
        "service": "asf",
        "version": app.version,
    }


@app.get("/")
async def root() -> dict:
    """Service root."""
    return {
        "service": "asf",
        "version": app.version,
        "docs": "/docs",
        "health": "/health",
    }


if __name__ == "__main__":  # pragma: no cover
    # Bind to 127.0.0.1 only — direct exposure is blocked by the
    # firewall; the only public path is the nginx basic-auth proxy on
    # 8086. This is the same loopback-only convention used by the
    # minimal skeleton.
    import uvicorn  # local import: uvicorn is required at runtime

    host = os.environ.get("ASF_HOST", "127.0.0.1")
    port = int(os.environ.get("ASF_SERVER_PORT", "8923"))
    uvicorn.run("asf.main:app", host=host, port=port)
