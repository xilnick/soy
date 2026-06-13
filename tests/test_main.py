"""
Tests for the ASF FastAPI app (``asf.main``).

The tests are PG-agnostic when run without
``ASF_TEST_DATABASE_URL`` (they only check that the routes are
registered), and exercise the lifespan migration hook when a
PostgreSQL test database is configured.
"""

from __future__ import annotations

import os

import pytest


def test_app_routes_registered():
    """The FastAPI app must expose ``/health`` and ``/``."""
    from asf.main import app

    paths = {r.path for r in app.routes}
    assert "/health" in paths
    assert "/" in paths
    assert "/openapi.json" in paths


def test_app_metadata():
    """The FastAPI app must declare a title and version."""
    from asf.main import app

    assert app.title
    assert app.version


def test_app_serves_health():
    """Smoke test: a TestClient request to /health returns 200."""
    from fastapi.testclient import TestClient
    from asf.main import app

    # ``ASF_RUN_MIGRATIONS_ON_STARTUP=false`` so we don't need a
    # database to serve /health. The lifespan hook logs and continues
    # even if migrations fail.
    os.environ["ASF_RUN_MIGRATIONS_ON_STARTUP"] = "false"
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "asf"


def test_lifespan_runs_migrations():
    """When a test database is configured, the lifespan hook must
    run ``alembic upgrade head`` and successfully serve /health."""
    test_db_url = os.getenv("ASF_TEST_DATABASE_URL", "").strip()
    if not test_db_url:
        pytest.skip("ASF_TEST_DATABASE_URL is not set")
    from fastapi.testclient import TestClient
    from asf.main import app

    # Re-use the existing test URL.
    os.environ["ASF_DATABASE_URL"] = test_db_url
    # Remove the disable flag so the hook runs.
    os.environ.pop("ASF_RUN_MIGRATIONS_ON_STARTUP", None)
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
