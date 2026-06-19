"""
Tests for the execution log read endpoints.

Covers the read-side of the execution surface. The write-side is
exercised by :mod:`soy.tests.test_tasks`; this module only
verifies that the GET endpoints serve the rows the worker
inserts.
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from soy.db import get_db
from soy.main import app
from soy.models import Agent, AgentRole, Execution, ExecutionStatus, Mission, Task, TaskStatus
from soy.models.base import Base
import soy.models  # noqa: F401


# ---------------------------------------------------------------------------
# PraisonAI stub — keep the router tests PG- and LLM-agnostic.
# ---------------------------------------------------------------------------
class _StubPraisonAgent:
    def __init__(self, *args, **kwargs):
        pass


class _StubPraisonTask:
    def __init__(self, *args, **kwargs):
        pass


class _StubWorkflow:
    behaviour = None

    def __init__(self, *args, **kwargs):
        pass

    def start(self, *args, **kwargs):
        b = _StubWorkflow.behaviour
        if b is None:
            return {"ok": True}
        return b()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def engine(tmp_path, monkeypatch):
    import os
    db_path = tmp_path / "soy_test.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("SOY_DATABASE_URL", url)
    from soy import db as db_mod
    from soy.services.praisonai_worker import reset_worker

    reset_worker()
    db_mod.reset_engine()
    eng = create_engine(
        url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def client(session_factory, monkeypatch) -> Iterator[TestClient]:
    def _get_db_override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    monkeypatch.setenv("SOY_RUN_MIGRATIONS_ON_STARTUP", "false")
    monkeypatch.setattr("praisonaiagents.Agent", _StubPraisonAgent, raising=False)
    monkeypatch.setattr("praisonaiagents.Task", _StubPraisonTask, raising=False)
    monkeypatch.setattr("praisonaiagents.Agents", _StubWorkflow, raising=False)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_mission(client) -> dict:
    r = client.post(
        "/api/v1/missions",
        json={
            "title": "M",
            "description": "d",
            "repo_url": "https://github.com/example/repo",
            "branch_prefix": "feature/soy-1",
        },
    )
    assert r.status_code == 201
    return r.json()


def _create_agent(client, mission_id, role="coder") -> dict:
    r = client.post(
        f"/api/v1/missions/{mission_id}/agents",
        json={"name": f"a-{role}", "role": role},
    )
    assert r.status_code == 201
    return r.json()


def _create_task(client, mission_id, agent_id) -> dict:
    r = client.post(
        f"/api/v1/missions/{mission_id}/agents/{agent_id}/tasks",
        json={"description": "x", "expected_output": "y"},
    )
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/executions
# ---------------------------------------------------------------------------
def test_list_executions_for_mission(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])

    with session_factory() as db:
        for attempt in range(1, 4):
            db.add(Execution(
                task_id=uuid.UUID(task["id"]),
                agent_id=uuid.UUID(agent["id"]),
                mission_id=uuid.UUID(mission["id"]),
                status=(
                    ExecutionStatus.failed if attempt < 3
                    else ExecutionStatus.completed
                ),
                attempt_number=attempt,
                output={"n": attempt},
            ))
        db.commit()

    r = client.get(f"/api/v1/missions/{mission['id']}/executions")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert [e["attempt_number"] for e in body["items"]] == [1, 2, 3]


def test_list_executions_filter_by_status(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    with session_factory() as db:
        for attempt, status_value in enumerate(
            [ExecutionStatus.failed, ExecutionStatus.completed, ExecutionStatus.failed],
            start=1,
        ):
            db.add(Execution(
                task_id=uuid.UUID(task["id"]),
                agent_id=uuid.UUID(agent["id"]),
                mission_id=uuid.UUID(mission["id"]),
                status=status_value,
                attempt_number=attempt,
            ))
        db.commit()

    r = client.get(
        f"/api/v1/missions/{mission['id']}/executions?status=failed"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(e["status"] == "failed" for e in body["items"])


def test_get_execution_returns_404_on_wrong_mission(client, session_factory):
    m1 = _create_mission(client)
    m2 = client.post(
        "/api/v1/missions",
        json={
            "title": "M2",
            "description": "d",
            "repo_url": "https://github.com/example/repo2",
            "branch_prefix": "feature/soy-2",
        },
    ).json()
    agent = _create_agent(client, m1["id"])
    task = _create_task(client, m1["id"], agent["id"])
    with session_factory() as db:
        e = Execution(
            task_id=uuid.UUID(task["id"]),
            agent_id=uuid.UUID(agent["id"]),
            mission_id=uuid.UUID(m1["id"]),
            status=ExecutionStatus.completed,
            attempt_number=1,
        )
        db.add(e)
        db.commit()
        eid = e.id

    r = client.get(f"/api/v1/missions/{m2['id']}/executions/{eid}")
    assert r.status_code == 404
    assert r.json()["code"] == "EXECUTION_NOT_FOUND"


def test_list_task_executions_returns_attempt_order(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    with session_factory() as db:
        for attempt in (3, 1, 2):  # insert out of order
            db.add(Execution(
                task_id=uuid.UUID(task["id"]),
                agent_id=uuid.UUID(agent["id"]),
                mission_id=uuid.UUID(mission["id"]),
                status=ExecutionStatus.failed,
                attempt_number=attempt,
            ))
        db.commit()
    r = client.get(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/executions"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert [e["attempt_number"] for e in body["items"]] == [1, 2, 3]
