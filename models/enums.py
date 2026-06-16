"""
soy.models.enums
================

Python enum types for ASF status/role columns.

These enums are the single source of truth used by both the SQLAlchemy
ORM models (declared with ``Enum(MyEnum, name=...)``) and the Pydantic
schemas in the API layer. The enum *value* stored in the database is
the string ``member.value``; PostgreSQL receives a native ``CREATE
TYPE ... AS ENUM (...)`` via the Alembic migration, and a CHECK
constraint is added for SQLite and any other backend that does not
support native enums.
"""

from __future__ import annotations

import enum


class MissionStatus(str, enum.Enum):
    """Lifecycle state of a mission.

    Valid transitions are enforced by the API state machine; the
    database CHECK constraint prevents any value outside this set from
    ever being inserted, even by raw SQL.
    """

    created = "created"
    planning = "planning"
    approved = "approved"
    rejected = "rejected"
    execution = "execution"
    reviewed = "reviewed"
    merged = "merged"
    escalated = "escalated"


class AgentRole(str, enum.Enum):
    """Role of an agent within a mission.

    The set is intentionally small; the PraisonAI worker maps each
    role to a separate ``Agent`` instance so that Coder and Reviewer
    have adversarial separation (see architecture.md).
    """

    coder = "coder"
    qa = "qa"
    reviewer = "reviewer"
    orchestrator = "orchestrator"


class AgentStatus(str, enum.Enum):
    """Lifecycle state of an agent."""

    idle = "idle"
    working = "working"
    paused = "paused"
    failed = "failed"
    completed = "completed"


class TaskStatus(str, enum.Enum):
    """Lifecycle state of a task."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    escalated = "escalated"


class ExecutionStatus(str, enum.Enum):
    """Lifecycle state of a single execution attempt.

    The 3-try retry rule (see architecture.md) keys off this status:
    ``failed`` is retryable, ``escalated`` is terminal.
    """

    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    timeout = "timeout"


class ApprovalDecision(str, enum.Enum):
    """Human review decision for an approval gate."""

    approve = "approve"
    reject = "reject"


class ApprovalGateType(str, enum.Enum):
    """Which gate a decision applies to.

    * ``planning`` — after the spec is written, before execution starts.
    * ``merge``    — after adversarial review, before the branch is
      merged into the default branch.
    """

    planning = "planning"
    merge = "merge"


class ChatSenderType(str, enum.Enum):
    """Origin of a chat message.

    * ``user``   — a human in Mission Control or the CLI.
    * ``agent``  — a PraisonAI agent (the row's ``sender_id`` is a
      FK into ``agents.id``).
    * ``system`` — a system-generated message (the ``sender_id`` is
      NULL because there is no specific actor).
    """

    user = "user"
    agent = "agent"
    system = "system"
