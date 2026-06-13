"""
asf.models.execution
===================

``Execution`` ORM model.

An ``Execution`` is a single attempt of a single task. The 3-try
retry policy (see architecture.md) is enforced at the application
layer by counting rows with the same ``task_id``; the database simply
records every attempt with a monotonically increasing ``attempt_number``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    JSON,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from asf.models.base import Base, TimestampMixin, Uuid
from asf.models.enums import ExecutionStatus

if TYPE_CHECKING:
    from asf.models.agent import Agent
    from asf.models.mission import Mission
    from asf.models.task import Task


class Execution(Base, TimestampMixin):
    """A single execution attempt of a task.

    Columns:

    * ``id``              — UUID primary key.
    * ``task_id``         — FK into ``tasks.id`` (cascade).
    * ``agent_id``        — FK into ``agents.id`` (cascade).
    * ``mission_id``      — FK into ``missions.id`` (cascade).
                            Denormalised for fast per-mission log
                            queries.
    * ``status``          — Lifecycle state (see
                            :class:`ExecutionStatus`).
    * ``attempt_number``  — 1-based count of which attempt this is.
    * ``output``          — Free-form result body (Markdown / JSON).
    * ``error``           — Error trace if ``status`` is
                            ``failed``/``timeout``/``cancelled``.
    * ``started_at``      — When the attempt began.
    * ``finished_at``     — When the attempt ended (NULL while
                            ``status`` is ``running``).
    """

    __tablename__ = "executions"
    __table_args__ = (
        Index("ix_executions_task_id", "task_id"),
        Index("ix_executions_agent_id", "agent_id"),
        Index("ix_executions_mission_id", "mission_id"),
        Index("ix_executions_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ExecutionStatus] = mapped_column(
        SAEnum(
            ExecutionStatus,
            name="execution_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
        default=ExecutionStatus.queued,
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    output: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- relationships -------------------------------------------------
    task: Mapped["Task"] = relationship(back_populates="executions")
    agent: Mapped["Agent"] = relationship(back_populates="executions")
    mission: Mapped["Mission"] = relationship(back_populates="executions")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Execution id={self.id!s} task={self.task_id!s} "
            f"attempt={self.attempt_number} status={self.status!r}>"
        )
