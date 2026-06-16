"""
soy.models.mission
==================

``Mission`` ORM model.

A ``Mission`` is the top-level unit of work in the AI Software Factory.
It corresponds to a single software engineering task — typically
ingested from a GitHub issue — and orchestrates a DAG of agents and
tasks from creation to merge.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Enum as SAEnum,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from soy.models.base import Base, TimestampMixin, Uuid
from soy.models.enums import MissionStatus

if TYPE_CHECKING:
    from soy.models.agent import Agent
    from soy.models.approval import Approval
    from soy.models.chat_message import ChatMessage
    from soy.models.execution import Execution
    from soy.models.task import Task


class Mission(Base, TimestampMixin):
    """Top-level ASF orchestration record.

    Columns:

    * ``id``              — UUID primary key, generated client-side.
    * ``issue_id``        — External identifier (e.g. GitHub issue
                            number). Nullable so manually-issued
                            missions are supported.
    * ``repo_url``        — Source repository the mission targets.
    * ``branch``          — Feature branch the agent team works on
                            (``feature/asf-<issue_id>``).
    * ``title``           — Human-readable summary.
    * ``description``     — Full problem statement.
    * ``status``          — Lifecycle state (see :class:`MissionStatus`).
    * ``spec_path``       — Path on the branch to the spec.md the
                            research agent wrote. Nullable until the
                            planning phase writes it.
    * ``source``          — Where the mission came from
                            (``"github_issue"`` | ``"manual"``).
    * ``external_id``     — Source-system identifier (e.g. issue
                            number) for idempotent ingestion.
    * ``spec_commit_sha`` — Git SHA of the commit that introduced
                            ``spec.md``. Set by the Git ops service.
    * ``merge_commit_sha``— Git SHA of the merge commit (only
                            populated after the merge gate).
    * ``metadata``        — JSONB blob for ad-hoc structured data
                            (e.g. webhook payload, label set, custom
                            fields). The application reads it via the
                            ``->`` operator or SQLAlchemy ``.op()``.
    """

    __tablename__ = "missions"
    __table_args__ = (
        # B-tree index on status for filter queries
        Index("ix_missions_status", "status"),
        # B-tree index on created_at for "latest missions" lists
        Index("ix_missions_created_at", "created_at"),
        # Database-level guard: status must be a known enum value.
        CheckConstraint(
            "status IN ('created','planning','approved','rejected',"
            "'execution','reviewed','merged','escalated')",
            name="ck_missions_status",
        ),
        # Uniqueness on (repo_url, branch_prefix). The constraint is
        # declared with NULL-distinct semantics: when either column is
        # NULL, the unique check skips that row, so the application
        # can still create ad-hoc missions without a repo URL. The
        # Alembic migration uses a partial index on PostgreSQL to make
        # the same semantic explicit; the SQLAlchemy ``unique=True``
        # form is rendered as a regular UNIQUE constraint, which the
        # migration replaces with a partial index for PostgreSQL.
        UniqueConstraint(
            "repo_url", "branch_prefix",
            name="uq_missions_repo_url_branch_prefix",
        ),
        # Idempotent ingestion guard: at most one mission per
        # (source, external_id) when external_id is present. A partial
        # unique index (``WHERE external_id IS NOT NULL``) lets ad-hoc
        # missions without an external id coexist freely while making
        # re-delivered GitHub-issue webhooks race-safe at the DB level
        # (the router also pre-checks for the common sequential case).
        Index(
            "uq_missions_source_external_id",
            "source", "external_id",
            unique=True,
            sqlite_where=text("source IS NOT NULL AND external_id IS NOT NULL"),
            postgresql_where=text(
                "source IS NOT NULL AND external_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    issue_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    repo_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    branch_prefix: Mapped[Optional[str]] = mapped_column(
        String(128), nullable=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[MissionStatus] = mapped_column(
        SAEnum(
            MissionStatus,
            name="mission_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            native_enum=True,
        ),
        nullable=False,
        default=MissionStatus.created,
    )
    spec_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    spec_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    merge_commit_sha: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # JSONB on PostgreSQL, JSON on SQLite (for unit tests / dev).
    mission_metadata: Mapped[Optional[dict]] = mapped_column(
        "metadata",
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=True,
    )

    # --- relationships -------------------------------------------------
    # cascade="all, delete-orphan" ensures that deleting a mission
    # cascades to its child rows. The Alembic migration declares the
    # same cascade behaviour at the database level (ON DELETE CASCADE)
    # for the foreign keys.
    agents: Mapped[List["Agent"]] = relationship(
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tasks: Mapped[List["Task"]] = relationship(
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    approvals: Mapped[List["Approval"]] = relationship(
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    chat_messages: Mapped[List["ChatMessage"]] = relationship(
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    executions: Mapped[List["Execution"]] = relationship(
        back_populates="mission",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return (
            f"<Mission id={self.id!s} title={self.title!r} "
            f"status={self.status!r}>"
        )
