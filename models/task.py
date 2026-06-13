"""
asf.models.task
===============

``Task`` ORM model.

A ``Task`` is a unit of work assigned to a specific agent. Tasks form a
DAG inside a mission: each task may declare zero or more upstream
dependencies, encoded as a JSON list of task IDs in ``depends_on``.

The PraisonAI worker translates each row into a ``praisonaiagents.Task``
instance and submits it to the ``AgentTeam``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    JSON,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from asf.models.base import Base, TimestampMixin, Uuid
from asf.models.enums import TaskStatus

if TYPE_CHECKING:
    from asf.models.agent import Agent
    from asf.models.execution import Execution
    from asf.models.mission import Mission


class Task(Base, TimestampMixin):
    """A unit of work assigned to an agent.

    Columns:

    * ``id``              — UUID primary key.
    * ``mission_id``      — FK into ``missions.id`` (cascade).
    * ``agent_id``        — FK into ``agents.id`` (cascade). The
                            agent that will execute the task.
    * ``description``     — Free-form task body (Markdown).
    * ``expected_output`` — Description of what the agent should
                            produce (passed to ``Task.expected_output``).
    * ``status``          — Lifecycle state (see :class:`TaskStatus`).
    * ``depends_on``      — JSONB list of upstream task UUIDs. The
                            worker only enqueues the task after every
                            dependency has reached ``completed``.
    * ``attempt_count``   — How many times this task has been tried
                            (read by the retry policy).
    * ``config``          — JSONB of task-level configuration
                            (timeouts, retry policy, sandbox flag).
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_mission_id", "mission_id"),
        Index("ix_tasks_agent_id", "agent_id"),
        Index("ix_tasks_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("missions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    expected_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(
            TaskStatus,
            name="task_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
        default=TaskStatus.pending,
    )
    # depends_on is a JSON list of UUIDs (string form on SQLite).
    depends_on: Mapped[Optional[list]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    config: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )

    # --- relationships -------------------------------------------------
    mission: Mapped["Mission"] = relationship(back_populates="tasks")
    agent: Mapped["Agent"] = relationship(back_populates="tasks")
    executions: Mapped[List["Execution"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Task id={self.id!s} status={self.status!r} "
            f"attempts={self.attempt_count}>"
        )
