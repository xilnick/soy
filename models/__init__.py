"""
asf.models
==========

SQLAlchemy ORM models for ASF. Re-exports the ``Base`` declarative
class, every concrete model, and the Python enum types used by the
schema.

Importing this package materialises the metadata used by Alembic to
generate migrations. The ``asf.alembic.env`` module imports
``asf.models`` so that ``Base.metadata`` is fully populated before any
DDL is generated.
"""

from asf.models.base import Base
from asf.models.enums import (
    AgentRole,
    AgentStatus,
    ApprovalDecision,
    ApprovalGateType,
    ChatSenderType,
    ExecutionStatus,
    MissionStatus,
    TaskStatus,
)
from asf.models.mission import Mission
from asf.models.agent import Agent
from asf.models.task import Task
from asf.models.execution import Execution
from asf.models.approval import Approval
from asf.models.chat_message import ChatMessage

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
