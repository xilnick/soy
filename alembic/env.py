"""
================================================================================
ASF Alembic Environment
================================================================================
Standard Alembic env.py with two important twists:

1. The database URL is read at runtime from the ``ASF_DATABASE_URL``
   environment variable (so the same migration script works against
   local SQLite, the test PostgreSQL container, and the production
   PostgreSQL instance without ever baking a secret into the repo).

2. The ``Base.metadata`` is imported from :mod:`asf.models`, which
   means *all* ASF models are part of the autogenerate target — there
   is no risk of a new model being added without its table being
   included in the next migration.

Run migrations with::

    cd /Users/purplelephant/projects/piperoni
    ASF_DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/asf \
        alembic --config asf/alembic.ini upgrade head

Or, equivalently, invoke the ``run_alembic_upgrade`` helper from
``asf.db`` (used by the FastAPI lifespan hook and the deploy script).
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure the project root is on sys.path so ``asf`` can be imported when
# Alembic is invoked from any working directory. This is necessary
# because the configured script_location is the package-relative path
# ``asf/alembic`` and Alembic does not automatically add the project
# root to ``sys.path``.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Importing asf.models populates Base.metadata with every table.
# This must happen *after* sys.path is adjusted above.
from asf.models import Base  # noqa: E402

# Alembic Config object provides access to alembic.ini values.
config = context.config

# Configure Python logging from alembic.ini if available.
#
# ``disable_existing_loggers=False`` is important: migrations are run
# IN-PROCESS by the FastAPI ``lifespan`` hook (via
# ``asf.db.run_alembic_upgrade``). The default ``fileConfig`` behaviour
# (``disable_existing_loggers=True``) would tear down every already-
# configured ``asf.*`` logger the moment the app runs its startup
# migration, silently breaking the application's structured logging
# (and, in tests, ``caplog`` capture for any logger configured before a
# migration ran). Preserving existing loggers keeps both intact.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Metadata for autogenerate support.
target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Read the database URL with environment-aware fallback.

    Order of resolution:

    1. ``ASF_DATABASE_URL`` env var (always wins; set by the deploy
       blueprint's ``.env`` file).
    2. The ``sqlalchemy.url`` field in alembic.ini (almost always
       empty, present only so ``alembic check`` does not error).
    3. A SQLite file in the current directory, so the offline mode
       and ``alembic check`` work in a clean checkout.
    """
    env_url = os.getenv("ASF_DATABASE_URL", "").strip()
    if env_url:
        return env_url
    ini_url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if ini_url:
        return ini_url
    return "sqlite:///./asf_dev.db"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Emits SQL to stdout instead of executing it. Used by ``alembic
    upgrade head --sql`` to dump the migration script for review.
    """
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection.

    Used by ``alembic upgrade head`` (no --sql flag). The connection
    is created with ``engine_from_config`` so connection-pool settings
    from alembic.ini are honoured.
    """
    # Inject the resolved URL into the config so engine_from_config
    # picks it up. We do not mutate ``config`` permanently — we just
    # override the in-memory value for this run.
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _resolve_database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
