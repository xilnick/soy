"""
soy.models.agent
================

``Agent`` ORM model.

An ``Agent`` belongs to a mission and represents a single PraisonAI
agent instance. Each role (coder, qa, reviewer, orchestrator) is a
separate row so that the API can list and audit them independently.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from soy.models.base import Base, TimestampMixin, Uuid
from soy.models.enums import AgentRole, AgentStatus

if TYPE_CHECKING:
    from soy.models.execution import Execution
    from soy.models.mission import Mission
    from soy.models.task import Task


class Agent(Base, TimestampMixin):
    """A PraisonAI agent scoped to a single mission.

    Columns:

    * ``id``          — UUID primary key.
    * ``mission_id``  — FK into ``missions.id`` (cascade delete).
    * ``name``        — Display name (e.g. ``"coder"``).
    * ``role``        — One of the :class:`AgentRole` enum members.
    * ``model``       — LLM identifier the agent was instantiated with
                        (e.g. ``"ollama/codestral"``).
    * ``llm_config``  — Optional dict of provider-specific options
                        (temperature, top_p, etc.).
    * ``status``      — Lifecycle state (see :class:`AgentStatus`).
    * ``tool_config`` — JSONB describing the tools the agent can call
                        (``file_read``, ``file_write``, etc.). The
                        sandbox flag toggles whether ``run_command``
                        and ``web_search`` are included.
    * ``system_prompt``— Optional override for the agent's system
                        prompt (used to give the Reviewer a paranoid
                        framing without changing the role enum).
    """

    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_mission_id", "mission_id"),
        Index("ix_agents_role", "role"),
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
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[AgentRole] = mapped_column(
        SAEnum(
            AgentRole,
            name="agent_role",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
    )
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    llm_config: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )
    status: Mapped[AgentStatus] = mapped_column(
        SAEnum(
            AgentStatus,
            name="agent_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
        default=AgentStatus.idle,
    )
    tool_config: Mapped[Optional[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sandbox: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # --- relationships -------------------------------------------------
    mission: Mapped["Mission"] = relationship(back_populates="agents")
    tasks: Mapped[List["Task"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    executions: Mapped[List["Execution"]] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Agent id={self.id!s} name={self.name!r} role={self.role!r}>"
