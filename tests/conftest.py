"""Pytest configuration shared by the ASF test suite.

The tests need to import the ``asf`` package regardless of where
pytest is invoked from. We add the project root to ``sys.path`` so
``import asf.models`` works when running ``pytest asf/tests/`` from
the project root, or ``pytest`` from any subdirectory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the piperoni project root."""
    return _PROJECT_ROOT


@pytest.fixture(autouse=True)
def _reset_db_engine(request):
    """Reset the cached engine + worker between tests.

    Several test modules build their own SQLAlchemy engines
    against an in-memory SQLite database. The ASF worker caches
    a separate engine on first use, so the test would see two
    different engines unless we reset the cache before and
    after each test. The fixture is autouse so every test
    starts with a clean engine cache.

    We use ``request`` as a parameter so the fixture is
    guaranteed to run as the first fixture of every test
    (pytest evaluates fixtures in the order they appear, and
    the autouse fixture is treated as the first requested).
    """
    import sys
    try:
        from asf import db as db_mod
        from asf.services.praisonai_worker import reset_worker

        reset_worker()
        db_mod.reset_engine()
        # Also drop the cached sessionmaker explicitly so the
        # next ``get_session_local()`` rebuilds the engine
        # from the current env var.
        _asf_db = db_mod
        _asf_db._SessionLocal = None
        _asf_db._engine = None
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    yield
    try:
        from asf import db as db_mod
        from asf.services.praisonai_worker import reset_worker

        reset_worker()
        db_mod.reset_engine()
        _asf_db = db_mod
        _asf_db._SessionLocal = None
        _asf_db._engine = None
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
