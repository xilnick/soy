"""
soy.models.chat_message
======================

``ChatMessage`` ORM model.

A ``ChatMessage`` is a single line of PM Chat. The ``sender_type`` is
an enum (``user`` | ``agent`` | ``system``); when ``sender_type`` is
``agent`` the ``sender_id`` is a foreign key into ``agents.id``,
otherwise it is NULL.

The FK is declared as ``ON DELETE SET NULL`` (rather than CASCADE) so
that deleting an agent does not destroy its historical chat messages —
the row simply becomes anonymous.
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

from soy.models.base import Base, TimestampMixin, Uuid
from soy.models.enums import ChatSenderType

if TYPE_CHECKING:
    from soy.models.mission import Mission


class ChatMessage(Base, TimestampMixin):
    """A single PM-Chat line attached to a mission.

    Columns:

    * ``id``              — UUID primary key.
    * ``mission_id``      — FK into ``missions.id`` (cascade).
    * ``sender_type``     — One of :class:`ChatSenderType` (``user``,
                            ``agent``, ``system``).
    * ``sender_id``       — UUID of the sender when ``sender_type``
                            is ``agent`` (FK into ``agents.id``,
                            ON DELETE SET NULL). NULL for ``user``
                            and ``system`` messages.
    * ``sender_name``     — Display name snapshot at write time
                            (e.g. ``"admin"``, ``"coder"``,
                            ``"system"``). Stored so deleting an
                            agent does not blank the chat history.
    * ``content``         — The message body.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_mission_id", "mission_id"),
        Index("ix_chat_messages_sender_type", "sender_type"),
        Index("ix_chat_messages_created_at", "created_at"),
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
    sender_type: Mapped[ChatSenderType] = mapped_column(
        SAEnum(
            ChatSenderType,
            name="chat_sender_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
    )
    sender_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    sender_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # --- relationships -------------------------------------------------
    mission: Mapped["Mission"] = relationship(back_populates="chat_messages")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ChatMessage id={self.id!s} sender_type={self.sender_type!r} "
            f"mission={self.mission_id!s}>"
        )
