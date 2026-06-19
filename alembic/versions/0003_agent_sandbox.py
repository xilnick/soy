"""SOY — add agent.sandbox column

Revision ID: 0003_agent_sandbox
Revises: 0002_branch_prefix_and_unique
Create Date: 2026-06-04 00:00:00.000000

Adds a ``sandbox`` boolean column to the ``agents`` table. The
column toggles whether an agent receives the unrestricted tool
list (``file_read`` + ``file_write`` + ``run_command`` +
``web_search``) or the sandboxed tool list (``file_read`` +
``file_write`` only). See :mod:`soy.services.praisonai_worker` for
the runtime tool resolution.

Revision 0003 owns the column's entire lifecycle: 0001 deliberately
does NOT create it (mirroring how 0002 owns ``branch_prefix``), so
``upgrade`` actually adds the column and ``downgrade`` cleanly
reverses it. The upgrade is still guarded by an idempotency check so
re-running ``alembic upgrade head`` against a database that already
has the column (e.g. one whose schema was bootstrapped from the ORM
``create_all`` rather than the migration chain) is a no-op. New rows
default to ``TRUE`` (sandboxed) so the safe default is preserved.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_agent_sandbox"
down_revision: Union[str, None] = "0002_branch_prefix_and_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``sandbox`` to ``agents`` (idempotent)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("agents"):
        # The agents table does not exist yet — 0001 (which creates
        # it) has not run. The revision chain guarantees 0001 runs
        # first, so this is only reachable on a partially-initialised
        # database; nothing to add.
        return
    existing_columns = {
        col["name"] for col in inspector.get_columns("agents")
    }
    if "sandbox" not in existing_columns:
        op.add_column(
            "agents",
            sa.Column(
                "sandbox",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade() -> None:
    """Remove the ``sandbox`` column from ``agents``."""
    op.drop_column("agents", "sandbox")
