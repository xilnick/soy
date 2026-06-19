"""
soy.db
======

Database connection helpers for Soy.

The same module is used by the FastAPI app (``soy.main``), the Alembic
``env.py`` environment, and unit tests. It centralises the SQLAlchemy
engine, session factory, and a FastAPI-friendly ``get_db`` dependency.

The database URL is read from the ``SOY_DATABASE_URL`` environment
variable (which the Piperoni deploy blueprint writes into
``~/repos/soy/.env``). When the variable is not set, the module falls back to
``sqlite:///./soy_dev.db`` so that local development and the Alembic
``offline`` mode work without a running PostgreSQL container.
"""

from __future__ import annotations

import logging
import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("soy.db")

# Default fallback (used only when SOY_DATABASE_URL is not set). On the
# VPS the deploy blueprint always writes the PostgreSQL URL into
# ~/repos/soy/.env so this fallback is never reached in production.
_DEFAULT_SQLITE_URL = "sqlite:///./soy_dev.db"


def get_database_url() -> str:
    """Return the SQLAlchemy database URL, honoring SOY_DATABASE_URL.

    The value is read at call time (not module import time) so the
    Piperoni deploy can write ``~/repos/soy/.env`` *before* the FastAPI
    process starts and have the new URL picked up by Alembic.
    """
    url = os.getenv("SOY_DATABASE_URL", "").strip()
    if url:
        return url
    return _DEFAULT_SQLITE_URL


def _build_engine(url: str) -> Engine:
    """Create a SQLAlchemy engine with sensible defaults.

    * ``pool_pre_ping`` is enabled so dead connections from a stopped
      PostgreSQL container are recycled automatically on the next use.
    * For SQLite, ``check_same_thread`` is disabled so the same engine
      can be shared by the FastAPI thread pool (and the Alembic
      single-thread executor).
    """
    if url.startswith("sqlite"):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            future=True,
        )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        future=True,
    )


# Engine and session factory are created lazily so importing ``soy.db``
# in environments without a configured database (e.g. lightweight unit
# tests that mock the engine) does not raise.
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the lazily-initialised SQLAlchemy engine.

    Re-reads the ``SOY_DATABASE_URL`` env var on first call. Subsequent
    calls reuse the cached engine. Tests that need a different URL
    should call :func:`reset_engine` after adjusting the env var.
    """
    global _engine, _SessionLocal
    if _engine is None:
        url = get_database_url()
        _engine = _build_engine(url)
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=_engine,
            expire_on_commit=False,
            future=True,
        )
    return _engine


def reset_engine() -> None:
    """Dispose of the current engine and clear the cache.

    Useful for tests that swap ``SOY_DATABASE_URL`` between cases. After
    calling this, the next call to :func:`get_engine` rebuilds the
    engine from the current env.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_session_local() -> sessionmaker[Session]:
    """Return the cached session factory, building the engine if needed."""
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_session_factory() -> sessionmaker[Session]:
    """FastAPI dependency that returns the session *factory*.

    Unlike :func:`get_db` (which yields a single live ``Session`` for
    the request's lifetime), this returns the ``sessionmaker`` itself
    so a caller can open one or more short-lived sessions on demand.

    WebSocket handlers use this: a long-lived connection should not
    pin a pooled DB connection for its whole lifetime, so the handler
    opens a session only for the brief existence check and releases it
    immediately. Routing the lookup through a dependency (rather than
    reading the module-global :func:`get_session_local` directly) also
    keeps the handler test-injectable via ``app.dependency_overrides``.
    """
    return get_session_local()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a session and closes it on exit."""
    session_factory = get_session_local()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Alembic invocation helper
# ---------------------------------------------------------------------------
def _locate_alembic_ini() -> str:
    """Return the absolute path to ``soy/alembic.ini`` on disk.

    The path is computed relative to this file (``soy/db.py``) so the
    helper works regardless of the process's current working directory.
    """
    soy_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(soy_dir, "alembic.ini")


def run_alembic_upgrade(
    target: str = "head",
    database_url: str | None = None,
) -> None:
    """Programmatically run ``alembic upgrade <target>``.

    Used by the FastAPI ``lifespan`` hook and the Piperoni deploy
    blueprint. The function configures the Alembic ``Config`` object
    in-process, so it does not need to shell out to the ``alembic``
    CLI binary — and therefore does not need ``alembic`` on the
    operator's PATH (it does, however, need to be importable, which
    is guaranteed by ``requirements.txt``).

    Parameters
    ----------
    target:
        Alembic revision target, default ``"head"``. Pass an explicit
        revision ID (e.g. ``"0001_initial_schema"``) to pin the
        upgrade.
    database_url:
        Override for the database URL. When ``None`` (the default),
        the value is read from the ``SOY_DATABASE_URL`` env var, with
        the same SQLite fallback used by :func:`get_database_url`.
    """
    # Local imports: alembic is a runtime dependency but we keep the
    # import here so test harnesses that mock the engine do not have
    # to install alembic just to import ``soy.db``.
    from alembic import command
    from alembic.config import Config

    url = database_url or get_database_url()
    cfg = Config(_locate_alembic_ini())
    # ``cfg.set_main_option`` mutates the in-memory config; the file
    # on disk is left untouched.
    cfg.set_main_option("sqlalchemy.url", url)
    # ``cmd_kwargs`` would suppress stdout — we want to see the
    # standard "Running upgrade ..." lines in the PM2 log.
    logger.info("Running Alembic upgrade to %s on %s", target, _safe_url(url))
    command.upgrade(cfg, target)


def _safe_url(url: str) -> str:
    """Return a redacted form of a database URL for logging.

    Strips the password component so credentials never appear in
    PM2 / journal logs. SQLite URLs (which have no password) are
    returned unchanged.
    """
    try:
        # Format example: ``postgresql+psycopg2://user:passwd@host:port/dbname``
        if "@" not in url:
            return url
        scheme, rest = url.split("://", 1)
        if "@" not in rest:
            return url
        auth, host = rest.split("@", 1)
        if ":" in auth:
            user, _ = auth.split(":", 1)
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://{auth}@{host}"
    except Exception:  # noqa: BLE001 — never raise from a logger helper
        return "<unparseable database url>"
