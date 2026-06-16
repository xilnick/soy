"""
Tests for the SQLAlchemy ORM models.

These tests do not require a running database — they validate that the
declarative classes are well-formed (correct column types, foreign
keys, indexes, enums). The model-level invariants (PK presence,
timestamp columns, JSONB vs JSON) are cross-checked by
:mod:`soy.tests.test_migrations` against a live PostgreSQL.
"""

from __future__ import annotations

import os

import pytest

# These tests only inspect the declarative metadata — they do not
# touch a live database. They are skipped when the ``asf`` package
# cannot be imported (which would indicate a broken environment).
pytestmark = pytest.mark.skipif(
    not os.path.exists(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "asf", "models", "__init__.py",
        )
    ),
    reason="ASF models package is not present",
)


# ---------------------------------------------------------------------------
# Smoke test: importing the package must populate Base.metadata.
# ---------------------------------------------------------------------------
def test_models_import_populates_metadata():
    import soy.models as models

    expected = {
        "missions", "agents", "tasks", "executions",
        "approvals", "chat_messages",
    }
    actual = set(models.Base.metadata.tables.keys())
    assert expected <= actual, f"Missing tables: {expected - actual}"


def test_mission_status_enum_values():
    """Every value in ``MissionStatus`` must be a valid Python identifier."""
    from soy.models.enums import MissionStatus

    assert {m.value for m in MissionStatus} == {
        "created", "planning", "approved", "rejected",
        "execution", "reviewed", "merged", "escalated",
    }


def test_agent_role_enum_values():
    from soy.models.enums import AgentRole

    assert {m.value for m in AgentRole} == {
        "coder", "qa", "reviewer", "orchestrator",
    }


def test_chat_sender_type_enum_values():
    from soy.models.enums import ChatSenderType

    assert {m.value for m in ChatSenderType} == {"user", "agent", "system"}


def test_mission_columns_present():
    from soy.models import Mission

    column_names = {c.name for c in Mission.__table__.columns}
    # The Python attribute for the JSONB column is
    # ``mission_metadata``; the underlying DB column is named
    # ``metadata`` (chosen so SQL stays human-readable).
    expected = {
        "id", "title", "status", "metadata",
        "created_at", "updated_at", "spec_path", "branch",
        "repo_url", "issue_id", "description", "source",
        "external_id", "spec_commit_sha", "merge_commit_sha",
    }
    assert expected <= column_names, (
        f"Missing mission columns: {expected - column_names}"
    )
    # And the Python attribute is reachable on an instance.
    m = Mission(title="x", mission_metadata={"k": "v"})
    assert m.mission_metadata == {"k": "v"}


def test_agents_have_tool_config_column():
    from soy.models import Agent

    column_names = {c.name for c in Agent.__table__.columns}
    assert "tool_config" in column_names
    assert "system_prompt" in column_names
    assert "mission_id" in column_names
    assert "llm_config" in column_names


def test_chat_messages_sender_id_is_nullable():
    from soy.models import ChatMessage

    sender_id = ChatMessage.__table__.columns["sender_id"]
    assert sender_id.nullable is True


def test_chat_messages_sender_type_not_nullable():
    from soy.models import ChatMessage

    sender_type = ChatMessage.__table__.columns["sender_type"]
    assert sender_type.nullable is False


def test_every_table_has_timestamps():
    """Every ASF table must inherit TimestampMixin (created_at + updated_at)."""
    from soy.models import (
        Agent, Approval, ChatMessage, Execution, Mission, Task,
    )

    tables = (Agent, Approval, ChatMessage, Execution, Mission, Task)
    for cls in tables:
        cols = {c.name for c in cls.__table__.columns}
        assert "created_at" in cols, f"{cls.__name__} missing created_at"
        assert "updated_at" in cols, f"{cls.__name__} missing updated_at"


def test_relationships_declared():
    """Sanity check: every model that owns a relationship declares it."""
    from soy.models import Mission

    rel_names = {r.key for r in Mission.__mapper__.relationships}
    assert {"agents", "tasks", "approvals", "chat_messages", "executions"} <= rel_names
