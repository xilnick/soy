"""ASF — add branch_prefix and (repo_url, branch_prefix) uniqueness

Revision ID: 0002_branch_prefix_and_unique
Revises: 0001_initial_schema
Create Date: 2026-06-04 00:00:00.000000

Adds the ``branch_prefix`` column to ``missions`` and enforces a
uniqueness constraint on the combination ``(repo_url, branch_prefix)``.

The constraint is declared as a partial unique index on PostgreSQL so
that rows where either column is NULL are *not* considered
duplicates — this is the semantics the application relies on
(``branch_prefix`` is optional; ad-hoc missions without a repo
URL are allowed). On SQLite we use a regular unique index because
SQLite supports index creation on existing tables but does not
support adding a UNIQUE constraint via ALTER TABLE; the test suite
runs against SQLite.

The migration is guarded by ``checkfirst`` on the index creation so
re-running ``alembic upgrade head`` against a database that already
has the column is a no-op.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_branch_prefix_and_unique"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``branch_prefix`` and the (repo_url, branch_prefix) unique index."""
    bind = op.get_bind()

    # 1. Add the column if it does not exist.
    inspector = sa.inspect(bind)
    existing_columns = {
        col["name"] for col in inspector.get_columns("missions")
    } if inspector.has_table("missions") else set()
    if "branch_prefix" not in existing_columns:
        op.add_column(
            "missions",
            sa.Column("branch_prefix", sa.String(length=128), nullable=True),
        )

    # 2. Add the unique index. We use a unique index (not a
    #    constraint) so the statement works on both PostgreSQL and
    #    SQLite. On PostgreSQL the index is a *partial* unique
    #    index — it ignores rows where either column is NULL, which
    #    is the application's contract (ad-hoc missions without a
    #    repo URL are allowed to share a NULL branch_prefix).
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        # SQLite: regular unique index. ``create_index`` is
        # idempotent thanks to ``IF NOT EXISTS`` in the rendered DDL.
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_missions_repo_url_branch_prefix
            ON missions (repo_url, branch_prefix)
            """
        )
    else:
        # PostgreSQL: partial unique index that ignores NULLs.
        op.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_missions_repo_url_branch_prefix
            ON missions (repo_url, branch_prefix)
            WHERE repo_url IS NOT NULL AND branch_prefix IS NOT NULL
            """
        )


def downgrade() -> None:
    """Remove the (repo_url, branch_prefix) uniqueness and the column."""
    op.execute("DROP INDEX IF EXISTS uq_missions_repo_url_branch_prefix")
    op.drop_column("missions", "branch_prefix")

