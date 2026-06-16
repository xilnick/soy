"""
soy.tests
=========

Unit tests for the ASF domain — primarily the SQLAlchemy models, the
Alembic migration, and the JSONB / ENUM / FK invariants. These tests
are fast and require only the ASF package itself plus SQLAlchemy +
Alembic. They are designed to be run with ``pytest soy/tests/`` from
the project root.

By default the tests use a local PostgreSQL test database if one is
available (URL via ``ASF_TEST_DATABASE_URL``); if not, they fall back
to a SQLite file in a temporary directory so the suite still runs in
CI environments without Postgres.
"""

__all__: list[str] = []
