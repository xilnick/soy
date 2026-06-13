"""
SQLite-backed Alembic migration-chain tests.

Unlike :mod:`asf.tests.test_migrations` (which is PostgreSQL-gated and
validates native ENUM / JSONB behaviour), these run on a throwaway
SQLite file so they execute in CI without a database. They guard two
specific regressions:

* ``test_sandbox_migration_is_reversible`` — revision 0003 owns the
  ``agents.sandbox`` column's full lifecycle (0001 no longer creates
  it), so ``downgrade -1`` actually removes it and ``upgrade`` re-adds
  it. Previously 0001 created the column and 0003.downgrade dropped it
  unconditionally, leaving revision 0002 path-dependent.
* ``test_source_external_id_unique_enforced`` — the partial unique
  index on ``missions (source, external_id)`` rejects a duplicate
  ingestion at the database level (defence-in-depth behind the
  router's idempotency pre-check).
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import asf.models  # noqa: F401 — register tables on Base.metadata
from asf.models.base import Base
from asf.models.enums import MissionStatus
from asf.models.mission import Mission


def _alembic_cfg(url: str):
    from alembic.config import Config

    cfg = Config("asf/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def sqlite_url(tmp_path):
    return f"sqlite:///{tmp_path / 'chain.db'}"


def _agents_columns(url: str) -> set[str]:
    eng = create_engine(url)
    try:
        insp = inspect(eng)
        if not insp.has_table("agents"):
            return set()
        return {c["name"] for c in insp.get_columns("agents")}
    finally:
        eng.dispose()


def test_sandbox_migration_is_reversible(sqlite_url):
    from alembic import command

    cfg = _alembic_cfg(sqlite_url)
    command.upgrade(cfg, "head")
    assert "sandbox" in _agents_columns(sqlite_url)

    # Downgrade one step must REMOVE the column (it is 0003's to own).
    command.downgrade(cfg, "0002_branch_prefix_and_unique")
    assert "sandbox" not in _agents_columns(sqlite_url)

    # Re-upgrading restores it — the round trip is clean.
    command.upgrade(cfg, "head")
    assert "sandbox" in _agents_columns(sqlite_url)

    command.downgrade(cfg, "base")
    assert _agents_columns(sqlite_url) == set()


def test_source_external_id_unique_enforced(sqlite_url):
    """Two missions with the same (source, external_id) cannot coexist."""
    eng = create_engine(sqlite_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, future=True)
    try:
        with Session() as s:
            s.add(Mission(
                title="m1", source="github", external_id="issue-1",
                status=MissionStatus.created,
            ))
            s.commit()
        with Session() as s:
            s.add(Mission(
                title="m2", source="github", external_id="issue-1",
                status=MissionStatus.created,
            ))
            with pytest.raises(IntegrityError):
                s.commit()
        # Rows WITHOUT an external_id are exempt (partial index).
        with Session() as s:
            s.add(Mission(title="a", source="manual", status=MissionStatus.created))
            s.add(Mission(title="b", source="manual", status=MissionStatus.created))
            s.commit()  # no error — external_id IS NULL on both
    finally:
        eng.dispose()
