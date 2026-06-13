"""
asf.models.approval
==================

``Approval`` ORM model.

An ``Approval`` is a single human review decision attached to a mission
gate. The ``mission.merged`` transition is gated on at least one
``approve`` row of type ``merge``; the ``mission.planning`` transition
is gated on an ``approve`` row of type ``planning``.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from asf.models.base import Base, TimestampMixin, Uuid
from asf.models.enums import ApprovalDecision, ApprovalGateType

if TYPE_CHECKING:
    from asf.models.mission import Mission


class Approval(Base, TimestampMixin):
    """A human review decision for a mission gate.

    Columns:

    * ``id``              — UUID primary key.
    * ``mission_id``      — FK into ``missions.id`` (cascade).
    * ``gate_type``       — Which gate the decision applies to
                            (see :class:`ApprovalGateType`).
    * ``decision``        — ``approve`` or ``reject``.
    * ``reviewer_notes``  — Free-form feedback.
    * ``approved_by``     — Identifier of the reviewer (e.g. Mission
                            Control user UUID or ``"system"``).
    * ``rejection_reason``— Populated when ``decision`` is ``reject``.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_mission_id", "mission_id"),
        Index("ix_approvals_decision", "decision"),
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
    gate_type: Mapped[ApprovalGateType] = mapped_column(
        SAEnum(
            ApprovalGateType,
            name="approval_gate_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
    )
    decision: Mapped[ApprovalDecision] = mapped_column(
        SAEnum(
            ApprovalDecision,
            name="approval_decision",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
    )
    reviewer_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- relationships -------------------------------------------------
    mission: Mapped["Mission"] = relationship(back_populates="approvals")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<Approval id={self.id!s} gate={self.gate_type!r} "
            f"decision={self.decision!r}>"
        )
