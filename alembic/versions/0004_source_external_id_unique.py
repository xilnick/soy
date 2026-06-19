"""SOY — idempotent ingestion: unique (source, external_id)

Revision ID: 0004_source_external_id_unique
Revises: 0003_agent_sandbox
Create Date: 2026-06-06 00:00:00.000000

Adds a *partial* unique index on ``missions (source, external_id)``
that applies only when BOTH ``source`` and ``external_id`` are
non-NULL (a complete ingestion key). This makes re-delivery of the
same source-system identifier (e.g. a GitHub issue webhook fired
twice) race-safe at the database level: a concurrent second insert
with the same ``(source, external_id)`` is rejected, and the router
returns the already-ingested mission. Rows without a ``source`` or
``external_id`` (ad-hoc / manual missions) are excluded from the
index, so they are never collapsed together and can coexist freely.

Both SQLite (the test backend) and PostgreSQL support partial
indexes, so the same ``WHERE`` clause is used on both. The
``IF NOT EXISTS`` guard makes re-running ``alembic upgrade head`` a
no-op.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_source_external_id_unique"
down_revision: Union[str, None] = "0003_agent_sandbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the partial unique index on (source, external_id)."""
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_missions_source_external_id
        ON missions (source, external_id)
        WHERE source IS NOT NULL AND external_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop the (source, external_id) partial unique index."""
    op.execute("DROP INDEX IF EXISTS uq_missions_source_external_id")
