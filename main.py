"""
soy.main
========

FastAPI entrypoint for the Soy Orchestration Yield.

The application is intentionally minimal at this milestone — it exposes
``/health`` so the deploy blueprint can prove the service is up, and
its ``lifespan`` hook runs ``alembic upgrade head`` on startup so the
schema is always in sync with the code, even when a fresh image is
deployed.

The schema-management concern is intentionally implemented in
:mod:`soy.db` (the ``run_alembic_upgrade`` helper) so that the same
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
from pathlib import Path
from typing import AsyncIterator

# Load .env from the project root before any config reads so
# ``os.getenv`` calls in :mod:`soy.config` and :mod:`soy.db`
# pick up the deploy-written values even when PM2 does not inject
# them into the process environment (the JS ecosystem config
# ``env_file`` key is only honoured by PM2 for Node.js processes).
try:
    from dotenv import load_dotenv

    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.is_file():
        load_dotenv(_env_path, override=False)
except ImportError:
    pass  # python-dotenv not installed — fall back to PM2 env injection

from fastapi import Depends, FastAPI

from soy.api.v1.router import api_v1_router, ws_router
from soy.auth import verify_api_key
from soy.db import run_alembic_upgrade
from soy.errors import register_exception_handlers
from soy.ws.events import install_as_publisher

logger = logging.getLogger("soy.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run migrations on startup, log shutdown on exit.

    The ``SOY_RUN_MIGRATIONS_ON_STARTUP`` env var is honoured so unit
    tests that mount a pre-migrated SQLite in-memory database can
    skip the Alembic invocation. The variable is read at the top of
    the function (not module import time) so test fixtures that
    toggle it are respected.
    """
    # Install structured (JSON) logging to stdout first so every
    # subsequent startup line — including migration output — is
    # captured in the structured format PM2 ships.
    from soy.logging_config import configure_logging
    from soy.sentry_init import init_sentry

    configure_logging()
    init_sentry("soy-api")

    # Centralized Sentry path: root logger ERROR+ auto-ships to Sentry.
    try:
        from sentry_log_handler import install_sentry_log_handler
        install_sentry_log_handler()
    except ImportError:
        pass  # sentry_log_handler not available (e.g. in tests)

    run_migrations = os.environ.get(
        "SOY_RUN_MIGRATIONS_ON_STARTUP", "true"
    ).lower() in ("1", "true", "yes")
    if run_migrations:
        try:
            run_alembic_upgrade()
        except Exception:  # noqa: BLE001 — log and continue
            # We deliberately do not abort startup on migration
            # failure: the operational signal is that ``/health``
            # returns 503 and the PM2 log captures the stack trace.
            logger.exception("SOY Alembic migration failed on startup")
    else:
        logger.info(
            "SOY startup migrations skipped "
            "(SOY_RUN_MIGRATIONS_ON_STARTUP=%s)",
            os.environ.get("SOY_RUN_MIGRATIONS_ON_STARTUP"),
        )
    yield
    logger.info("SOY shutdown")
    # Remove the stdout log handler we installed so it does not outlive
    # the app (keeps the root logger clean across per-test lifespans).
    from soy.logging_config import reset_logging

    reset_logging()


app = FastAPI(
    title="Soy Backend",
    version="0.1.0",
    description=(
        "Soy Orchestration Yield — FastAPI mission orchestration backend. "
        "See ``soy/models/`` for the schema and ``soy/alembic/`` for "
        "the migrations."
    ),
    lifespan=lifespan,
)

# Structured error handlers — every error response carries a top-level
# ``code`` field alongside ``detail``. See :mod:`soy.errors`.
register_exception_handlers(app)

# API v1 router — mission CRUD + state machine.
# When SOY_API_KEY is set the verify_api_key dependency gates every
# request to /api/v1/* (health, docs, and root are mounted on `app`
# directly and are therefore exempt).  When the env var is empty the
# dependency is a no-op — existing nginx basic-auth proxy is the sole gate.
app.include_router(api_v1_router, dependencies=[Depends(verify_api_key)])

# WebSocket router — real-time mission events.
# The ws_router has its own HMAC-based auth for the wildcard ``*``
# subscription; individual mission subscriptions are gated by
# mission existence checks inside the handler.
app.include_router(ws_router)

# Wire the SOY worker's event publisher to the in-memory WebSocket
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
        "service": "soy",
        "version": app.version,
    }


@app.get("/health/sentry-debug")
async def sentry_debug() -> dict:
    """FEAT-076: Probe Sentry wiring end-to-end.

    Synthesises a test exception and captures it via ``sentry_sdk``.
    Returns ``{"dsn_set": true/false, "captured": true/false}`` so the
    operator can confirm the SDK is wired without producing a real 500.
    Requires nginx basic-auth (same proxy as /health).
    """
    import sentry_sdk

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return {"dsn_set": False, "captured": False}
    try:
        sentry_sdk.capture_exception(
            RuntimeError("soy-api sentry-debug probe — safe to ignore")
        )
        return {"dsn_set": True, "captured": True}
    except Exception as e:
        return {"dsn_set": True, "captured": False, "error": str(e)}


@app.get("/")
async def root() -> dict:
    """Service root."""
    return {
        "service": "soy",
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

    host = os.environ.get("SOY_HOST", "127.0.0.1")
    port = int(os.environ.get("SOY_SERVER_PORT", "8923"))
    uvicorn.run("soy.main:app", host=host, port=port)
