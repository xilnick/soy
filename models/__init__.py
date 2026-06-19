"""
soy.models
==========

SQLAlchemy ORM models for SOY. Re-exports the ``Base`` declarative
class, every concrete model, and the Python enum types used by the
schema.

Importing this package materialises the metadata used by Alembic to
generate migrations. The ``soy.alembic.env`` module imports
``soy.models`` so that ``Base.metadata`` is fully populated before any
DDL is generated.
"""

from soy.models.base import Base
from soy.models.enums import (
    AgentRole,
    AgentStatus,
    ApprovalDecision,
    ApprovalGateType,
    ChatSenderType,
    ExecutionStatus,
    MissionStatus,
    TaskStatus,
)
from soy.models.mission import Mission
from soy.models.agent import Agent
from soy.models.task import Task
from soy.models.execution import Execution
from soy.models.approval import Approval
from soy.models.chat_message import ChatMessage

__all__ = [
    "Base",
    # Enums
    "AgentRole",
    "AgentStatus",
    "ApprovalDecision",
    "ApprovalGateType",
    "ChatSenderType",
    "ExecutionStatus",
    "MissionStatus",
    "TaskStatus",
    # ORM models
    "Mission",
    "Agent",
    "Task",
    "Execution",
    "Approval",
    "ChatMessage",
]
