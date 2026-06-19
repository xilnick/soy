"""
soy.models.base
===============

SQLAlchemy ``DeclarativeBase`` for SOY.

A single ``Base`` is shared by every model in ``soy.models`` so that
Alembic can read ``Base.metadata`` and autogenerate DDL diffs. The
convention here matches modern SQLAlchemy 2.x (no legacy ``declarative_base()``
call): every model inherits from this class and declares typed
attributes (``Mapped[...]``) on the new API.

The :class:`Uuid` type is a thin wrapper around SQLAlchemy's
``UUID`` / ``CHAR(36)`` types. It exposes UUID semantics on
PostgreSQL (``UUID`` column type) and a 36-character string on
SQLite (so the test suite can run without a running database) while
converting Python ``uuid.UUID`` objects to and from the database
representation on the SQLite side.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import CHAR, DateTime, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """Return a timezone-aware UTC ``datetime``.

    Used as the default value for ``created_at`` and ``updated_at``
    columns. Using ``datetime.utcnow`` is deprecated in Python 3.12+
    and SQLAlchemy 2.x expects timezone-aware values when the column
    is ``DateTime(timezone=True)``.
    """
    return datetime.now(timezone.utc)


class Uuid(TypeDecorator):
    """A UUID-typed column that works on both PostgreSQL and SQLite.

    On PostgreSQL the column is a native ``UUID`` type. On SQLite
    (and any other backend that does not support native UUID), the
    column is rendered as ``CHAR(36)`` and values are converted
    to/from Python ``uuid.UUID`` objects on the fly.

    The implementation deliberately mirrors the existing
    ``postgresql.UUID(as_uuid=True).with_variant(String(36),
    "sqlite")`` pattern but adds the SQLite-side conversion the
    original declaration was missing. Without the conversion,
    SQLAlchemy binds a ``uuid.UUID`` object directly to the SQLite
    statement, which raises ``sqlite3.ProgrammingError: Error
    binding parameter 1: type 'UUID' is not supported``.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(
        self, value: Optional[uuid.UUID], dialect: Any
    ) -> Optional[Any]:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(
        self, value: Optional[Any], dialect: Any
    ) -> Optional[uuid.UUID]:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class Base(DeclarativeBase):
    """Declarative base for every SOY ORM model.

    All tables in the SOY schema inherit from this class so that
    ``Base.metadata`` is the single source of truth used by Alembic to
    autogenerate migrations.
    """


class TimestampMixin:
    """Mixin that adds ``created_at`` and ``updated_at`` columns.

    Every SOY table has these two columns. The values are timezone-
    aware UTC timestamps. ``updated_at`` is initialised to the same
    value as ``created_at``; the application layer is responsible for
    bumping it on update (we deliberately do not use SQLAlchemy
    ``onupdate`` to keep migration output deterministic).
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
