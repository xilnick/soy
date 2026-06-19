"""Tests for the control dashboard endpoints (soy.api.v1.control)."""

from __future__ import annotations

import uuid
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from soy.models.mission import Mission
from soy.models.enums import MissionStatus


@pytest.fixture
def engine(monkeypatch):
    """Per-test in-memory SQLite engine."""
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="soy_test_")
    os.close(fd)
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
    )
    from soy.models.base import Base
    Base.metadata.create_all(eng)
    yield eng
    reset_worker()
    db_mod.reset_engine()
    os.unlink(db_path)


@pytest.fixture
def client(engine, monkeypatch):
    """FastAPI TestClient bound to the per-test engine."""
    from soy import db as db_mod
    from soy.main import app

    from sqlalchemy.orm import sessionmaker
    from soy.models.base import Base

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False,
    )

    def _override():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[db_mod.get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_control_payload(**overrides):
    payload = {
        "title": "Test Dashboard Mission",
        "description": "A mission created from the control dashboard",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions — create from dashboard
# ---------------------------------------------------------------------------
def test_create_control_mission_returns_201(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    assert body["status"] == "created"
    assert body["title"] == "Test Dashboard Mission"
    assert body["source"] == "dashboard"
    # repo_url and branch_prefix should be None
    assert body["repo_url"] is None
    assert body["branch_prefix"] is None


def test_create_control_mission_title_only(client):
    r = client.post(
        "/api/v1/control/missions",
        json={"title": "Minimal mission"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["title"] == "Minimal mission"
    assert body["description"] is None


def test_create_control_mission_with_source(client):
    r = client.post(
        "/api/v1/control/missions",
        json=_create_control_payload(source="api"),
    )
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "api"


def test_create_control_mission_missing_title_returns_422(client):
    r = client.post("/api/v1/control/missions", json={})
    assert r.status_code == 422


def test_create_control_mission_with_metadata(client):
    metadata = {"priority": "high", "tags": ["urgent"]}
    r = client.post(
        "/api/v1/control/missions",
        json=_create_control_payload(mission_metadata=metadata),
    )
    assert r.status_code == 201, r.text
    mission_id = r.json()["id"]
    r2 = client.get(f"/api/v1/missions/{mission_id}")
    assert r2.status_code == 200
    assert r2.json()["metadata"]["priority"] == "high"


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/refine
# ---------------------------------------------------------------------------
def test_refine_mission_records_metadata(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/refine",
        json={"prompt": "Make it more specific"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    md = body["metadata"]
    assert "refinement_history" in md
    assert len(md["refinement_history"]) == 1
    assert md["refinement_history"][0]["prompt"] == "Make it more specific"


def test_refine_mission_default_model(client, monkeypatch):
    monkeypatch.setenv("SOY_MODEL", "test-model:cloud")
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/refine",
        json={},
    )
    assert r2.status_code == 200
    history = r2.json()["metadata"]["refinement_history"]
    assert history[0]["model"] == "test-model:cloud"


def test_refine_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(
        f"/api/v1/control/missions/{fake_id}/refine",
        json={},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/research
# ---------------------------------------------------------------------------
def test_research_mission_records_metadata(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/research",
        json={"query": "search for rate limiting patterns"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    md = body["metadata"]
    assert "research" in md
    assert len(md["research"]) == 1
    assert md["research"][0]["query"] == "search for rate limiting patterns"
    assert md["research"][0]["status"] in ("triggered", "completed", "error")


def test_research_mission_default_query(client):
    r = client.post(
        "/api/v1/control/missions",
        json=_create_control_payload(description="Build a REST API"),
    )
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/research",
        json={},
    )
    assert r2.status_code == 200
    research = r2.json()["metadata"]["research"]
    assert "Test Dashboard Mission" in research[0]["query"]
    assert "Build a REST API" in research[0]["query"]


def test_research_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/control/missions/{fake_id}/research", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/verify
# ---------------------------------------------------------------------------
def test_verify_mission_records_metadata(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/verify",
        json={"prompt": "Check if the scope is achievable"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    md = body["metadata"]
    assert "verification" in md
    assert len(md["verification"]) == 1


def test_verify_mission_default_prompt(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/verify",
        json={},
    )
    assert r2.status_code == 200
    verification = r2.json()["metadata"]["verification"]
    assert verification[0]["prompt"] is None


def test_verify_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/control/missions/{fake_id}/verify", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/start-execution
# ---------------------------------------------------------------------------
def test_start_execution_from_created(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]
    assert r.json()["status"] == "created"

    r2 = client.post(f"/api/v1/control/missions/{mission_id}/start-execution")
    assert r2.status_code == 200
    assert r2.json()["status"] == "execution"


def test_start_execution_from_planning(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]
    client.post(
        f"/api/v1/missions/{mission_id}/transition",
        json={"to_status": "planning"},
    )

    r2 = client.post(f"/api/v1/control/missions/{mission_id}/start-execution")
    assert r2.status_code == 200
    assert r2.json()["status"] == "execution"


def test_start_execution_already_in_execution(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    client.post(f"/api/v1/control/missions/{mission_id}/start-execution")
    r2 = client.post(f"/api/v1/control/missions/{mission_id}/start-execution")
    assert r2.status_code == 200
    assert r2.json()["status"] == "execution"


def test_start_execution_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/control/missions/{fake_id}/start-execution")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/auto-run
# ---------------------------------------------------------------------------
def test_auto_run_requires_repo_url(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/auto-run",
        json={},
    )
    assert r2.status_code == 400
    assert r2.json()["code"] == "NO_REPO_URL"


def test_auto_run_with_repo_url_in_payload(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/auto-run",
        json={
            "repo_url": "/tmp/soy-test-repo",
            "branch_prefix": "feature/test",
            "auto_merge": False,
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] in ("error", "partial")


def test_auto_run_sets_repo_url_on_mission(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    client.post(
        f"/api/v1/control/missions/{mission_id}/auto-run",
        json={"repo_url": "/tmp/test-repo"},
    )

    r2 = client.get(f"/api/v1/missions/{mission_id}")
    assert r2.json()["repo_url"] == "/tmp/test-repo"


# ---------------------------------------------------------------------------
# GET /api/v1/control/missions/{id}/status
# ---------------------------------------------------------------------------
def test_control_status_returns_aggregated_data(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.get(f"/api/v1/control/missions/{mission_id}/status")
    assert r2.status_code == 200
    body = r2.json()
    assert body["mission_id"] == mission_id
    assert body["title"] == "Test Dashboard Mission"
    assert body["status"] == "created"
    assert body["agent_count"] == 0
    assert body["task_count"] == 0
    assert body["completed_tasks"] == 0
    assert body["last_execution"] is None


def test_control_status_with_agents(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    client.post(
        f"/api/v1/missions/{mission_id}/agents",
        json={"name": "coder-1", "role": "coder"},
    )

    r2 = client.get(f"/api/v1/control/missions/{mission_id}/status")
    assert r2.status_code == 200
    assert r2.json()["agent_count"] == 1


def test_control_status_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.get(f"/api/v1/control/missions/{fake_id}/status")
    assert r.status_code == 404


def test_control_status_shows_refinement_history(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    client.post(
        f"/api/v1/control/missions/{mission_id}/refine",
        json={"prompt": "improve it"},
    )

    r2 = client.get(f"/api/v1/control/missions/{mission_id}/status")
    assert r2.json()["refinement_history"] is not None
    assert len(r2.json()["refinement_history"]) == 1


def test_control_status_shows_research_results(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    client.post(
        f"/api/v1/control/missions/{mission_id}/research",
        json={},
    )

    r2 = client.get(f"/api/v1/control/missions/{mission_id}/status")
    assert r2.json()["research_results"] is not None


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/branch
# ---------------------------------------------------------------------------
def test_branch_requires_repo_url(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(f"/api/v1/control/missions/{mission_id}/branch")
    assert r2.status_code == 400
    assert r2.json()["code"] == "NO_REPO_URL"


def test_branch_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/control/missions/{fake_id}/branch")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/merge
# ---------------------------------------------------------------------------
def test_merge_requires_branch(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    r2 = client.post(
        f"/api/v1/control/missions/{mission_id}/merge",
        json={},
    )
    assert r2.status_code == 400
    assert r2.json()["code"] == "NO_BRANCH"


def test_merge_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/control/missions/{fake_id}/merge", json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/commit
# ---------------------------------------------------------------------------
def test_commit_unknown_mission_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/control/missions/{fake_id}/commit")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Integration: full dashboard workflow
# ---------------------------------------------------------------------------
def test_full_dashboard_workflow(client):
    """End-to-end: create -> refine -> research -> verify -> start-execution."""
    r = client.post(
        "/api/v1/control/missions",
        json=_create_control_payload(description="Initial description"),
    )
    assert r.status_code == 201
    mission_id = r.json()["id"]
    assert r.json()["status"] == "created"

    r = client.post(
        f"/api/v1/control/missions/{mission_id}/refine",
        json={"prompt": "Add error handling details"},
    )
    assert r.status_code == 200
    assert "refinement_history" in r.json()["metadata"]

    r = client.post(
        f"/api/v1/control/missions/{mission_id}/research",
        json={"query": "error handling best practices"},
    )
    assert r.status_code == 200
    assert "research" in r.json()["metadata"]

    r = client.post(
        f"/api/v1/control/missions/{mission_id}/verify",
        json={},
    )
    assert r.status_code == 200
    assert "verification" in r.json()["metadata"]

    r = client.post(f"/api/v1/control/missions/{mission_id}/start-execution")
    assert r.status_code == 200
    assert r.json()["status"] == "execution"

    r = client.get(f"/api/v1/control/missions/{mission_id}/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "execution"
    assert body["refinement_history"] is not None
    assert body["research_results"] is not None
    assert body["verification_results"] is not None


def test_multiple_refine_calls_accumulate_history(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    for i in range(3):
        client.post(
            f"/api/v1/control/missions/{mission_id}/refine",
            json={"prompt": f"iteration {i+1}"},
        )

    r2 = client.get(f"/api/v1/control/missions/{mission_id}/status")
    history = r2.json()["refinement_history"]
    assert len(history) == 3
    assert history[0]["prompt"] == "iteration 1"
    assert history[2]["prompt"] == "iteration 3"


def test_multiple_research_calls_accumulate_results(client):
    r = client.post("/api/v1/control/missions", json=_create_control_payload())
    mission_id = r.json()["id"]

    for i in range(2):
        client.post(
            f"/api/v1/control/missions/{mission_id}/research",
            json={"query": f"research topic {i+1}"},
        )

    r2 = client.get(f"/api/v1/control/missions/{mission_id}/status")
    research = r2.json()["research_results"]
    assert len(research) == 2
