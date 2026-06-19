"""
================================================================================
SOY (Soy Orchestration Yield)
================================================================================
FastAPI-based mission orchestration backend.

This package is the source-of-truth Python module for SOY. It is built
locally in the piperoni repository (not pulled from a remote git URL)
so that:

  * All domain code is committed to Git and reviewable in the same
    repository that provisions it.
  * Schema migrations, API routers, and PraisonAI integration evolve
    together.
  * The Piperoni deploy blueprint can run ``alembic upgrade head``
    directly against the local Python source — no git clone, no
    third-party build steps.

Subpackages:

  * ``soy.models``     — SQLAlchemy ORM models and Alembic Base.
  * ``soy.services``   — Domain services (PraisonAI worker, MC sync,
                          Git operations, DeerFlow trigger).
  * ``soy.api.v1``     — FastAPI routers for REST API v1.
  * ``soy.ws``         — WebSocket endpoint.
  * ``soy.alembic``    — Alembic environment + migration scripts.
  * ``soy.tests``      — pytest test suite.
"""

__version__ = "0.1.0"
