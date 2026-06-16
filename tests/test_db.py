"""
Tests for the ASF database helper module (``soy.db``).

These tests cover the URL resolution logic, the engine caching, and
the ``run_alembic_upgrade`` helper. The migration helper is exercised
in :mod:`soy.tests.test_migrations`; here we only test the URL
redaction + idempotency of the public API.
"""

from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def fresh_db_module(monkeypatch):
    """Import ``soy.db`` with a clean env and a stub engine cache."""
    # Drop any cached engine so the module re-reads ASF_DATABASE_URL.
    for mod in ("soy.db", "asf"):
        if mod in list(importlib.sys.modules):
            del importlib.sys.modules[mod]
    yield
    # Reset the cache after each test.
    if "soy.db" in list(importlib.sys.modules):
        from soy import db as db_mod

        db_mod.reset_engine()


def test_get_database_url_falls_back_to_sqlite(fresh_db_module, monkeypatch):
    monkeypatch.delenv("ASF_DATABASE_URL", raising=False)
    import soy.db as db_mod

    url = db_mod.get_database_url()
    assert url.startswith("sqlite:")


def test_get_database_url_honors_env(fresh_db_module, monkeypatch):
    monkeypatch.setenv("ASF_DATABASE_URL", "postgresql+psycopg2://x@y:5432/z")
    import soy.db as db_mod

    assert db_mod.get_database_url() == "postgresql+psycopg2://x@y:5432/z"


def test_engine_is_cached(fresh_db_module, monkeypatch):
    monkeypatch.setenv("ASF_DATABASE_URL", "sqlite:///:memory:")
    import soy.db as db_mod

    e1 = db_mod.get_engine()
    e2 = db_mod.get_engine()
    assert e1 is e2


def test_reset_engine_disposes(fresh_db_module, monkeypatch):
    monkeypatch.setenv("ASF_DATABASE_URL", "sqlite:///:memory:")
    import soy.db as db_mod

    e1 = db_mod.get_engine()
    db_mod.reset_engine()
    e2 = db_mod.get_engine()
    assert e1 is not e2


def test_safe_url_redacts_password(fresh_db_module):
    import soy.db as db_mod

    redacted = db_mod._safe_url("postgresql+psycopg2://user:secret@host:5432/db")
    assert "secret" not in redacted
    assert "user" in redacted
    assert "host" in redacted
    assert ":***@" in redacted


def test_safe_url_handles_no_password(fresh_db_module):
    import soy.db as db_mod

    assert db_mod._safe_url("sqlite:///./asf_dev.db") == "sqlite:///./asf_dev.db"


def test_safe_url_handles_no_at_sign(fresh_db_module):
    import soy.db as db_mod

    assert db_mod._safe_url("not a url") == "not a url"


def test_safe_url_handles_unparseable(fresh_db_module):
    import soy.db as db_mod

    # Should never raise; returns the unparseable marker.
    assert db_mod._safe_url("") == ""


def test_locate_alembic_ini_exists():
    import soy.db as db_mod

    path = db_mod._locate_alembic_ini()
    import os

    assert os.path.isfile(path)
    assert path.endswith("alembic.ini")


def test_run_alembic_upgrade_idempotent():
    """Running ``run_alembic_upgrade`` twice is a no-op the second time."""
    import soy.db as db_mod

    test_db = os.getenv("ASF_TEST_DATABASE_URL", "").strip()
    if not test_db:
        pytest.skip("ASF_TEST_DATABASE_URL is not set")
    # First run applies migrations; second run is a no-op.
    db_mod.run_alembic_upgrade(database_url=test_db)
    db_mod.run_alembic_upgrade(database_url=test_db)
