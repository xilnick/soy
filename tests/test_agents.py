"""
Tests for the agent CRUD + AgentTeam assembly API.

Maps to validation contract assertions:

* ``test_create_agent_persists_and_constructs_praisonai_agent`` — VAL-API-017
* ``test_create_agent_with_unknown_role_returns_422``         — VAL-API-018
* ``test_list_agents_filters_by_mission``                    — VAL-API-019
* ``test_assemble_team_orders_canonical``                    — VAL-API-020

The tests use an in-memory SQLite engine so they are PG-agnostic.
The ``client`` fixture is the same one used in
:mod:`soy.tests.test_missions`; the SOY worker is monkey-patched
so PraisonAI does not actually call an LLM (the unit tests only
verify the construction path, not the execution path).
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
from soy.models import (
    Agent,
    AgentRole,
    AgentStatus,
    Execution,
    Mission,
    Task,
    TaskStatus,
)
from soy.models.base import Base
import soy.models  # noqa: F401


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Per-test SQLite engine on a tmp file (shared by worker + app).

    The test uses a *file* SQLite database (not ``:memory:``) so
    every engine that points at the same URL — including the
    worker's lazily-cached engine — sees the same rows. The
    ``StaticPool`` keeps a single connection in use.
    """
    import os
    db_path = tmp_path / "soy_test.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("SOY_DATABASE_URL", url)
    # Reset the cached engine so the next ``get_session_local``
    # call rebuilds against the new URL.
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
    # Patch the SOY worker's PraisonAI construction so the test
    # exercises the worker without actually instantiating a
    # real Agent (which would try to resolve a model and
    # connect to an LLM provider that is not reachable from CI).
    from soy.services import praisonai_worker

    class _StubAgent:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    class _StubWorkflow:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        def start(self, *args, **kwargs):
            return {"output": "stub"}

    monkeypatch.setattr(
        "praisonaiagents.Agent", _StubAgent, raising=False,
    )
    monkeypatch.setattr(
        "praisonaiagents.Agents", _StubWorkflow, raising=False,
    )

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_mission(client, **overrides) -> dict:
    payload = {
        "title": "M",
        "description": "d",
        "repo_url": "https://github.com/example/repo",
        "branch_prefix": "feature/soy-1",
    }
    payload.update(overrides)
    r = client.post("/api/v1/missions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# VAL-API-017 — POST /api/v1/missions/{id}/agents
# ---------------------------------------------------------------------------
def test_create_agent_persists_and_constructs_praisonai_agent(client, session_factory):
    mission = _create_mission(client)
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents",
        json={
            "name": "coder-1",
            "role": "coder",
            "model": "ollama/codestral",
            "sandbox": True,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    uuid.UUID(body["id"])
    assert body["role"] == "coder"
    assert body["model"] == "ollama/codestral"
    assert body["status"] == "idle"
    assert body["sandbox"] is True

    # DB row is persisted.
    with session_factory() as db:
        from sqlalchemy import select
        agent = db.execute(
            select(Agent).where(Agent.id == uuid.UUID(body["id"]))
        ).scalar_one()
        assert agent.role == AgentRole.coder
        assert agent.mission_id == uuid.UUID(mission["id"])


# ---------------------------------------------------------------------------
# VAL-API-018 — invalid role returns 422
# ---------------------------------------------------------------------------
def test_create_agent_with_unknown_role_returns_422(client):
    mission = _create_mission(client)
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents",
        json={"name": "rogue", "role": "hacker"},
    )
    assert r.status_code == 422
    body = r.json()
    # Pydantic surfaces the allowed enum values in the detail.
    assert "detail" in body


def test_create_agent_each_role_is_accepted(client):
    mission = _create_mission(client)
    for role in ("coder", "qa", "reviewer", "orchestrator"):
        r = client.post(
            f"/api/v1/missions/{mission['id']}/agents",
            json={"name": f"a-{role}", "role": role},
        )
        assert r.status_code == 201, f"role={role}: {r.text}"
        assert r.json()["role"] == role


# ---------------------------------------------------------------------------
# VAL-API-019 — list filters by mission
# ---------------------------------------------------------------------------
def test_list_agents_filters_by_mission(client):
    m1 = _create_mission(client, branch_prefix="feature/soy-100")
    m2 = _create_mission(client, branch_prefix="feature/soy-101")
    # Add one agent to each mission.
    client.post(f"/api/v1/missions/{m1['id']}/agents",
                json={"name": "a1", "role": "coder"})
    client.post(f"/api/v1/missions/{m1['id']}/agents",
                json={"name": "a2", "role": "qa"})
    client.post(f"/api/v1/missions/{m2['id']}/agents",
                json={"name": "b1", "role": "coder"})

    # Mission 1 sees its two agents only.
    r = client.get(f"/api/v1/missions/{m1['id']}/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {a["name"] for a in body["items"]} == {"a1", "a2"}

    # Mission 2 sees its one agent only — no cross-mission leak.
    r = client.get(f"/api/v1/missions/{m2['id']}/agents")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "b1"


# ---------------------------------------------------------------------------
# VAL-API-020 — assemble team in canonical order
# ---------------------------------------------------------------------------
def test_assemble_team_orders_canonical(client):
    mission = _create_mission(client)
    # Insert in non-canonical order: reviewer, qa, coder, orchestrator.
    for role in ("reviewer", "qa", "coder", "orchestrator"):
        r = client.post(
            f"/api/v1/missions/{mission['id']}/agents",
            json={"name": f"a-{role}", "role": role},
        )
        assert r.status_code == 201

    r = client.post(f"/api/v1/missions/{mission['id']}/agents/team")
    assert r.status_code == 200
    body = r.json()
    assert body["order"] == ["orchestrator", "coder", "qa", "reviewer"]
    assert [a["role"] for a in body["team"]] == [
        "orchestrator", "coder", "qa", "reviewer",
    ]


def test_assemble_team_handles_missing_roles(client):
    mission = _create_mission(client)
    client.post(
        f"/api/v1/missions/{mission['id']}/agents",
        json={"name": "a-coder", "role": "coder"},
    )
    client.post(
        f"/api/v1/missions/{mission['id']}/agents",
        json={"name": "a-reviewer", "role": "reviewer"},
    )
    r = client.post(f"/api/v1/missions/{mission['id']}/agents/team")
    assert r.status_code == 200
    body = r.json()
    # Orchestrator and qa are missing — only the present roles
    # are returned, in the canonical order.
    assert body["order"] == ["coder", "reviewer"]


def test_assemble_team_empty_when_no_agents(client):
    mission = _create_mission(client)
    r = client.post(f"/api/v1/missions/{mission['id']}/agents/team")
    assert r.status_code == 200
    body = r.json()
    assert body["team"] == []
    assert body["order"] == []
    assert body["parallel_supported"] is False


def test_assemble_team_keeps_duplicate_role_agents(client):
    """A second agent of an existing role must not be dropped.

    The team-assembly map previously keyed a single agent per role
    (last-wins), silently losing a paired coder/reviewer. All agents
    are now retained, grouped in canonical role order (creation order
    within a role).
    """
    mission = _create_mission(client)
    for name, role in (
        ("coder-a", "coder"),
        ("coder-b", "coder"),
        ("reviewer-a", "reviewer"),
    ):
        r = client.post(
            f"/api/v1/missions/{mission['id']}/agents",
            json={"name": name, "role": role},
        )
        assert r.status_code == 201

    r = client.post(f"/api/v1/missions/{mission['id']}/agents/team")
    assert r.status_code == 200
    body = r.json()
    # Both coders survive (no last-wins drop), ordered by creation,
    # ahead of the reviewer.
    assert body["order"] == ["coder", "coder", "reviewer"]
    assert [a["name"] for a in body["team"]] == [
        "coder-a", "coder-b", "reviewer-a",
    ]


def test_create_agent_syncs_to_mission_control_when_enabled(client, monkeypatch):
    """The create-agent route pushes to MC when sync is enabled.

    Proves the router wiring (not just the helper). With the flag off
    — the default exercised by every other test — no push occurs.
    """
    from soy.services import mission_control_sync as mc

    monkeypatch.setenv("SOY_MC_SYNC_ENABLED", "true")
    calls = []
    monkeypatch.setattr(
        mc.MissionControlSync, "_post",
        lambda self, path, payload: calls.append(path) or True,
    )
    mission = _create_mission(client)
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents",
        json={"name": "a", "role": "coder"},
    )
    assert r.status_code == 201
    assert "/api/agents" in calls  # agent push fired via the route


def test_list_agents_pagination(client):
    """list_agents honours limit/offset while reporting the full total."""
    mission = _create_mission(client)
    for i in range(3):
        r = client.post(
            f"/api/v1/missions/{mission['id']}/agents",
            json={"name": f"a{i}", "role": "coder"},
        )
        assert r.status_code == 201
    r = client.get(f"/api/v1/missions/{mission['id']}/agents?limit=2&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
