"""
Tests for the mission CRUD + state-machine API.

Each test uses a fresh in-memory SQLite database so the assertions
are deterministic and isolated. The fixture is defined here (rather
than in ``conftest.py``) because the state-machine tests need a
schema-aware dependency-override on the FastAPI app, and importing
the routers in ``conftest.py`` would slow down the simpler unit
tests.
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
from soy.models.base import Base
import soy.models  # noqa: F401  — ensure tables are registered on Base.metadata
from soy.models.mission import Mission
from soy.state_machine import (
    ALLOWED_TRANSITIONS,
    ESCALATION_REJECTION_THRESHOLD,
    MissionStateMachine,
)
from soy.models.enums import MissionStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def engine(monkeypatch):
    """Per-test in-memory SQLite engine.

    ``StaticPool`` + ``check_same_thread=False`` is the standard
    pattern for sharing a single in-memory connection across
    threads (FastAPI's TestClient may invoke dependencies from
    different threads when using async).
    """
    import os
    import tempfile

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
    """A TestClient with the ``get_db`` dependency overridden."""

    def _get_db_override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    # Skip the lifespan migration hook — the in-memory schema is
    # already created by the ``engine`` fixture, and the hook would
    # try to run ``alembic upgrade head`` against the file-based
    # fallback URL.
    monkeypatch.setenv("SOY_RUN_MIGRATIONS_ON_STARTUP", "false")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_payload(**overrides) -> dict:
    payload = {
        "title": "Build feature X",
        "description": "Implement feature X end-to-end",
        "repo_url": "https://github.com/example/repo",
        "branch_prefix": "feature/soy-1",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# VAL-API-001: POST /api/v1/missions returns 201 with id, status, created_at, updated_at
# ---------------------------------------------------------------------------
def test_create_mission_returns_201_with_required_fields(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    uuid.UUID(body["id"])  # parses as UUID
    assert body["status"] == "created"
    assert body["title"] == "Build feature X"
    assert "created_at" in body
    assert "updated_at" in body
    # Followed-up GET returns the same row.
    r2 = client.get(f"/api/v1/missions/{body['id']}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "created"


# ---------------------------------------------------------------------------
# VAL-API-002: missing required fields returns 422 with Pydantic detail
# ---------------------------------------------------------------------------
def test_create_mission_missing_title_returns_422(client):
    payload = _create_payload()
    del payload["title"]
    r = client.post("/api/v1/missions", json=payload)
    assert r.status_code == 422
    body = r.json()
    assert "detail" in body
    # The detail is a list of error objects naming the missing field.
    assert any("title" in str(err) for err in body["detail"])


def test_create_mission_missing_repo_url_succeeds(client):
    # repo_url and branch_prefix are now optional for dashboard-first
    # mission creation (no GitHub required).
    payload = _create_payload()
    del payload["repo_url"]
    del payload["branch_prefix"]
    r = client.post("/api/v1/missions", json=payload)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["repo_url"] is None
    assert body["branch_prefix"] is None
    assert body["status"] == "created"


def test_create_mission_blank_required_field_returns_422(client):
    payload = _create_payload(title="")
    r = client.post("/api/v1/missions", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# VAL-API-003: list supports status filter, limit, offset, total, items
# ---------------------------------------------------------------------------
def test_list_missions_pagination_and_filter(client):
    # Create three missions with different statuses.
    created = []
    for i in range(3):
        r = client.post(
            "/api/v1/missions",
            json=_create_payload(
                title=f"M{i}",
                branch_prefix=f"feature/soy-{i}",
            ),
        )
        assert r.status_code == 201
        created.append(r.json())
    # Move one to planning.
    r = client.post(
        f"/api/v1/missions/{created[0]['id']}/transition",
        json={"to_status": "planning"},
    )
    assert r.status_code == 200

    # No filter, limit=2, offset=0.
    r = client.get("/api/v1/missions?limit=2&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 2
    assert body["offset"] == 0
    assert len(body["items"]) == 2

    # Status filter.
    r = client.get("/api/v1/missions?status=created&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert all(item["status"] == "created" for item in body["items"])

    r = client.get("/api/v1/missions?status=planning")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "planning"


# ---------------------------------------------------------------------------
# VAL-API-004: get single mission returns 200 or 404 with structured error
# ---------------------------------------------------------------------------
def test_get_mission_returns_200(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    r2 = client.get(f"/api/v1/missions/{mid}")
    assert r2.status_code == 200
    body = r2.json()
    assert body["id"] == mid
    assert "status" in body
    assert "created_at" in body
    assert "updated_at" in body


def test_get_mission_404_has_structured_code(client):
    missing = uuid.uuid4()
    r = client.get(f"/api/v1/missions/{missing}")
    assert r.status_code == 404
    body = r.json()
    # The error body has a machine-readable ``code`` field as the
    # validation contract requires.
    assert body["code"] == "MISSION_NOT_FOUND"
    assert "detail" in body


# ---------------------------------------------------------------------------
# VAL-API-005: PUT mutates only allowed fields, rejects direct status
# ---------------------------------------------------------------------------
def test_put_mission_updates_allowed_fields(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    r2 = client.put(
        f"/api/v1/missions/{mid}",
        json={"title": "Renamed"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["title"] == "Renamed"
    assert body["status"] == "created"  # unchanged

    # Followed-up GET reflects the change.
    r3 = client.get(f"/api/v1/missions/{mid}")
    assert r3.json()["title"] == "Renamed"


def test_put_mission_does_not_accept_status(client):
    """The PUT schema must not include ``status``.

    Pydantic silently ignores extra fields, so the request is
    accepted and the status remains unchanged. The point of this
    test is to assert that *no mutation of the status happens*.
    """
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    r2 = client.put(
        f"/api/v1/missions/{mid}",
        json={"title": "X", "status": "merged"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "created"
    # Confirm the rejection: ``status`` was not silently mutated.
    r3 = client.get(f"/api/v1/missions/{mid}")
    assert r3.json()["status"] == "created"


# ---------------------------------------------------------------------------
# VAL-API-006: DELETE cascades to agents, tasks, executions, approvals, chat
# ---------------------------------------------------------------------------
def test_delete_mission_cascades(client, session_factory):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]

    # Seed a few child rows directly via SQLAlchemy to ensure the
    # cascade covers every required table.
    from soy.models import (
        Agent, AgentRole, AgentStatus, Approval, ApprovalDecision,
        ApprovalGateType, ChatMessage, ChatSenderType, Execution,
        ExecutionStatus, Task, TaskStatus,
    )
    with session_factory() as db:
        m = db.get(__import__("soy.models.mission", fromlist=["Mission"]).Mission, uuid.UUID(mid))
        agent = Agent(
            mission_id=m.id, name="coder", role=AgentRole.coder,
            status=AgentStatus.idle,
        )
        db.add(agent)
        db.flush()
        task = Task(
            mission_id=m.id, agent_id=agent.id,
            description="do the thing", status=TaskStatus.pending,
        )
        db.add(task)
        db.flush()
        db.add(Execution(
            mission_id=m.id, task_id=task.id, agent_id=agent.id,
            status=ExecutionStatus.queued, attempt_number=1,
        ))
        db.add(Approval(
            mission_id=m.id, gate_type=ApprovalGateType.merge,
            decision=ApprovalDecision.approve,
        ))
        db.add(ChatMessage(
            mission_id=m.id, sender_type=ChatSenderType.user,
            content="hello",
        ))
        db.commit()

    # Delete via API.
    r = client.delete(f"/api/v1/missions/{mid}")
    assert r.status_code == 204

    # Confirm 404 on subsequent GET.
    r = client.get(f"/api/v1/missions/{mid}")
    assert r.status_code == 404

    # Confirm the child rows are gone.
    with session_factory() as db:
        from sqlalchemy import select, func
        from soy.models import (
            Agent, Approval, ChatMessage, Execution, Task,
        )
        for cls in (Agent, Approval, ChatMessage, Execution, Task):
            count = db.execute(
                select(func.count()).select_from(cls).where(cls.mission_id == uuid.UUID(mid))
            ).scalar_one()
            assert count == 0, f"{cls.__name__} rows leaked after mission delete"


# ---------------------------------------------------------------------------
# VAL-API-007: uniqueness enforced on (repo_url, branch_prefix) → 409
# ---------------------------------------------------------------------------
def test_uniqueness_returns_409(client):
    r1 = client.post(
        "/api/v1/missions",
        json=_create_payload(
            repo_url="https://github.com/example/repo",
            branch_prefix="feature/soy-7",
        ),
    )
    assert r1.status_code == 201
    # Second mission with the same repo+prefix collides.
    r2 = client.post(
        "/api/v1/missions",
        json=_create_payload(
            title="Different title",
            repo_url="https://github.com/example/repo",
            branch_prefix="feature/soy-7",
        ),
    )
    assert r2.status_code == 409
    body = r2.json()
    assert body["code"] == "MISSION_DUPLICATE"


def test_uniqueness_allows_different_branch_prefix(client):
    r1 = client.post(
        "/api/v1/missions",
        json=_create_payload(
            repo_url="https://github.com/example/repo",
            branch_prefix="feature/soy-A",
        ),
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/v1/missions",
        json=_create_payload(
            repo_url="https://github.com/example/repo",
            branch_prefix="feature/soy-B",
        ),
    )
    assert r2.status_code == 201


# ---------------------------------------------------------------------------
# VAL-API-008: state machine allows only defined transitions
# ---------------------------------------------------------------------------
def test_state_machine_allows_valid_transition(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "planning"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "planning"
    assert r2.json()["previous_status"] == "created"


def test_state_machine_rejects_invalid_transition(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # created -> merged is not allowed.
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "merged"},
    )
    assert r2.status_code == 400
    body = r2.json()
    assert body["code"] == "INVALID_TRANSITION"
    assert "allowed" in body
    # The only allowed transition from ``created`` is ``planning``.
    assert body["allowed"] == ["planning"]


# ---------------------------------------------------------------------------
# VAL-API-009: transition to planning triggers PraisonAI planning phase
# ---------------------------------------------------------------------------
def test_transition_to_planning_triggers_praisonai(client, session_factory):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "planning"},
    )
    assert r2.status_code == 200
    # The mission metadata should now include a "planning" entry
    # recording the trigger.
    with session_factory() as db:
        m = db.get(Mission, uuid.UUID(mid))
        assert m.status == MissionStatus.planning
        assert m.mission_metadata is not None
        assert "planning" in m.mission_metadata
        planning = m.mission_metadata["planning"]
        assert "model" in planning
        assert "started_at" in planning
        assert "praisonai_available" in planning


# ---------------------------------------------------------------------------
# VAL-API-010: transition to execution requires planning completion
# ---------------------------------------------------------------------------
def test_transition_to_execution_requires_planning_completion(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # Move to planning.
    client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "planning"},
    )
    # Try to move to execution without setting ``planning_complete``.
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "execution"},
    )
    assert r2.status_code == 403
    assert r2.json()["code"] == "PLANNING_INCOMPLETE"


def test_transition_to_execution_succeeds_when_planning_complete(client, session_factory):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "planning"},
    )
    # Mark planning complete via PUT.
    r2 = client.put(
        f"/api/v1/missions/{mid}",
        json={"mission_metadata": {"planning_complete": True}},
    )
    assert r2.status_code == 200
    # Now the planning→execution transition is allowed.
    r3 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "execution"},
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "execution"


# ---------------------------------------------------------------------------
# VAL-API-011: transition to reviewed requires execution completion
#             (audit_passed gating is enforced separately by the
#             adversarial-review feature; here we just confirm the
#             transition is accepted but flagged in metadata when
#             audit_passed is not yet set)
# ---------------------------------------------------------------------------
def test_transition_to_reviewed_with_execution(client, session_factory):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # created -> planning
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    # mark planning complete
    client.put(
        f"/api/v1/missions/{mid}",
        json={"mission_metadata": {"planning_complete": True}},
    )
    # planning -> execution
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"})
    # execution -> reviewed (no audit_passed yet — the API still
    # accepts the transition but records a note in the response).
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "reviewed"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "reviewed"


# ---------------------------------------------------------------------------
# VAL-API-012: transition to merged requires at least one approval
# ---------------------------------------------------------------------------
def test_transition_to_merged_requires_approval(client, session_factory):
    from soy.models import Approval, ApprovalDecision, ApprovalGateType
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # Drive the mission to reviewed.
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    client.put(
        f"/api/v1/missions/{mid}",
        json={"mission_metadata": {"planning_complete": True}},
    )
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"})
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "reviewed"})
    # Try merged without an approval.
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "merged"},
    )
    assert r2.status_code == 403
    assert r2.json()["code"] == "NO_APPROVAL"

    # Insert an approval row directly and retry.
    with session_factory() as db:
        db.add(Approval(
            mission_id=uuid.UUID(mid),
            gate_type=ApprovalGateType.merge,
            decision=ApprovalDecision.approve,
        ))
        db.commit()
    r3 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "merged"},
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "merged"


# ---------------------------------------------------------------------------
# VAL-API-013: rejection endpoint moves mission back to planning
# ---------------------------------------------------------------------------
def test_rejection_moves_back_to_planning(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    client.put(
        f"/api/v1/missions/{mid}",
        json={"mission_metadata": {"planning_complete": True}},
    )
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"})
    r2 = client.post(
        f"/api/v1/missions/{mid}/reject",
        json={"target_status": "planning", "reason": "fix tests"},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "planning"
    assert body["rejection_count"] == 1


# ---------------------------------------------------------------------------
# VAL-API-014: 4th rejection triggers escalation
# ---------------------------------------------------------------------------
def test_fourth_rejection_escalates(client, session_factory):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # Drive to execution, then reject four times.
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    client.put(
        f"/api/v1/missions/{mid}",
        json={"mission_metadata": {"planning_complete": True}},
    )
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"})

    for i in range(1, 4):
        r2 = client.post(
            f"/api/v1/missions/{mid}/reject",
            json={"target_status": "planning", "reason": f"rej {i}"},
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "planning"
        # Move back to execution for the next iteration. The PUT
        # merges ``mission_metadata`` rather than replacing it, so
        # the rejection counter from the previous iteration survives.
        client.put(
            f"/api/v1/missions/{mid}",
            json={"mission_metadata": {"planning_complete": True}},
        )
        client.post(
            f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"},
        )
    # The 4th rejection should escalate.
    r3 = client.post(
        f"/api/v1/missions/{mid}/reject",
        json={"target_status": "planning", "reason": "last straw"},
    )
    assert r3.status_code == 200
    body = r3.json()
    assert body["status"] == "escalated"
    assert body["rejection_count"] == ESCALATION_REJECTION_THRESHOLD


# ---------------------------------------------------------------------------
# VAL-API-015: invalid transition returns structured error with allowed list
# ---------------------------------------------------------------------------
def test_invalid_transition_returns_structured_error(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # Move to planning.
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    # Try an illegal jump back to created.
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "created"},
    )
    assert r2.status_code == 400
    body = r2.json()
    assert body["code"] == "INVALID_TRANSITION"
    # The allowed list is a JSON array of strings, sorted.
    assert "allowed" in body
    assert isinstance(body["allowed"], list)
    assert all(isinstance(x, str) for x in body["allowed"])


# ---------------------------------------------------------------------------
# VAL-API-016: concurrent transition requests are serialised
# ---------------------------------------------------------------------------
def test_concurrent_transition_serialization(client, session_factory):
    """Concurrent transitions on the same mission produce a
    consistent end state.

    The test asserts the *consistent* outcome of two simultaneous
    transition requests for the same mission: the mission must end
    up in a valid state and the server must not raise. The strict
    "one succeeds, one fails" guarantee is provided by the
    ``SELECT ... FOR UPDATE`` lock at the database level (see
    :func:`soy.api.v1.missions._lock_mission_or_404`); on
    PostgreSQL the lock is honoured and the second request
    returns 400 ``INVALID_TRANSITION`` (or 409
    ``CONCURRENT_TRANSITION``). On SQLite the lock is a no-op, so
    the test cannot rely on the strict path. We instead verify
    the SQL the router emits: the lock query must include
    ``with_for_update``.

    The unit test for the strict one-succeeds-one-fails path
    requires a real RDBMS and is run as part of the live
    validation suite, not the unit suite.
    """
    # 1. Static check: the router's lock helper must emit a
    #    ``SELECT ... FOR UPDATE`` query. Without the lock the
    #    contract on PostgreSQL is unenforceable.
    from soy.api.v1.missions import _lock_mission_or_404
    import inspect
    src = inspect.getsource(_lock_mission_or_404)
    assert "with_for_update" in src

    # 2. Behavioural check: when run against a SQLite test database
    #    the lock is a no-op, so we instead verify that the
    #    server does not crash and the mission ends up in a
    #    coherent state. We drive the test sequentially — a real
    #    concurrency test would need a Postgres fixture, which is
    #    out of scope for the unit suite.
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    # First transition succeeds.
    r1 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "planning"},
    )
    assert r1.status_code == 200
    # Second, idempotent transition is rejected by the state
    # machine: ``planning → planning`` is a no-op and the
    # router returns 400 INVALID_TRANSITION.
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition",
        json={"to_status": "planning"},
    )
    assert r2.status_code == 400
    assert r2.json()["code"] == "INVALID_TRANSITION"
    # The mission is still in planning.
    r3 = client.get(f"/api/v1/missions/{mid}")
    assert r3.json()["status"] == "planning"


# ---------------------------------------------------------------------------
# Pure-Python state machine tests
# ---------------------------------------------------------------------------
def test_state_machine_pure_logic():
    sm = MissionStateMachine()
    for src, expected in ALLOWED_TRANSITIONS.items():
        assert set(sm.allowed_targets(src)) == set(expected)
    # should_escalate semantics: only the 4th rejection escalates.
    assert not sm.should_escalate(0)
    assert not sm.should_escalate(1)
    assert not sm.should_escalate(2)
    assert not sm.should_escalate(3)
    assert sm.should_escalate(4)
    assert sm.should_escalate(5)


def test_state_machine_rejects_same_state():
    sm = MissionStateMachine()
    r = sm.can_transition(MissionStatus.planning, MissionStatus.planning)
    assert not r.allowed
    assert r.reason == "no_op_transition"


# ---------------------------------------------------------------------------
# PraisonAI trigger tests
# ---------------------------------------------------------------------------
def test_praisonai_trigger_returns_expected_shape():
    from soy.services.praisonai_trigger import trigger_planning_phase
    out = trigger_planning_phase(
        uuid.uuid4(),
        title="t",
        description="d",
        model_name="ollama/codestral",
    )
    assert "triggered" in out
    assert "model" in out
    assert "started_at" in out
    assert out["model"]["model"] == "ollama/codestral"


# ---------------------------------------------------------------------------
# Security: the planning trigger must not persist the resolved API key
# ---------------------------------------------------------------------------
def test_planning_trigger_does_not_persist_api_key(client, monkeypatch):
    """The resolved cloud credential must never reach mission_metadata."""
    monkeypatch.setenv("SOY_MODEL", "kimi-k2.6:cloud")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-should-not-leak")
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"},
    )
    body = client.get(f"/api/v1/missions/{mid}").json()
    model = body["metadata"]["planning"]["model"]
    assert "api_key" not in model
    assert model["has_api_key"] is True
    # The secret must not appear anywhere in the serialised mission.
    import json as _json
    assert "sk-super-secret-should-not-leak" not in _json.dumps(body)


# ---------------------------------------------------------------------------
# Gate: reaching execution via planning->approved->execution must still
# require planning completion (no bypass).
# ---------------------------------------------------------------------------
def test_execution_gate_not_bypassable_via_approved(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    # planning -> approved (allowed, no gate)
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "approved"},
    )
    assert r2.status_code == 200
    # approved -> execution WITHOUT planning_complete must be blocked.
    r3 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"},
    )
    assert r3.status_code == 403
    assert r3.json()["code"] == "PLANNING_INCOMPLETE"


# ---------------------------------------------------------------------------
# Reject: escalation from planning goes through the validated path.
# ---------------------------------------------------------------------------
def test_reject_from_planning_escalates_on_fourth(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    # Reject from planning repeatedly; the mission bounces back to
    # planning until the 4th rejection escalates it (planning ->
    # rejected -> escalated, both edges validated).
    for i in range(1, 4):
        rr = client.post(
            f"/api/v1/missions/{mid}/reject",
            json={"target_status": "planning", "reason": f"rej {i}"},
        )
        assert rr.status_code == 200, rr.text
        assert rr.json()["status"] == "planning"
    final = client.post(
        f"/api/v1/missions/{mid}/reject",
        json={"target_status": "planning", "reason": "last"},
    )
    assert final.status_code == 200
    assert final.json()["status"] == "escalated"


# ---------------------------------------------------------------------------
# Approve endpoint (planning + merge gates)
# ---------------------------------------------------------------------------
def test_approve_planning_gate_unblocks_execution(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    # Approving the planning gate marks planning complete.
    ra = client.post(
        f"/api/v1/missions/{mid}/approve",
        json={"gate_type": "planning", "approved_by": "alice"},
    )
    assert ra.status_code == 201, ra.text
    assert ra.json()["decision"] == "approve"
    assert ra.json()["gate_type"] == "planning"
    # Execution is now unblocked.
    re = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"},
    )
    assert re.status_code == 200
    assert re.json()["status"] == "execution"


def test_approve_merge_gate_enables_merge_via_api(client):
    r = client.post("/api/v1/missions", json=_create_payload())
    mid = r.json()["id"]
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "planning"})
    client.post(
        f"/api/v1/missions/{mid}/approve", json={"gate_type": "planning"},
    )
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "execution"})
    client.post(f"/api/v1/missions/{mid}/transition", json={"to_status": "reviewed"})
    # Without the merge approval, merged is blocked.
    r1 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "merged"},
    )
    assert r1.status_code == 403
    assert r1.json()["code"] == "NO_APPROVAL"
    # The /approve endpoint creates the required merge approval row.
    ra = client.post(
        f"/api/v1/missions/{mid}/approve", json={"gate_type": "merge"},
    )
    assert ra.status_code == 201
    r2 = client.post(
        f"/api/v1/missions/{mid}/transition", json={"to_status": "merged"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "merged"


# ---------------------------------------------------------------------------
# Idempotent ingestion on (source, external_id)
# ---------------------------------------------------------------------------
def test_ingestion_idempotent_on_source_external_id(client):
    payload = _create_payload(source="github", external_id="issue-42")
    r1 = client.post("/api/v1/missions", json=payload)
    assert r1.status_code == 201
    first_id = r1.json()["id"]
    # Re-delivering the same webhook returns the SAME mission, no dup.
    r2 = client.post("/api/v1/missions", json=payload)
    assert r2.status_code == 201
    assert r2.json()["id"] == first_id
    listing = client.get("/api/v1/missions").json()
    assert listing["total"] == 1


def test_ingestion_without_source_is_not_deduped(client):
    """external_id with no source is NOT a dedup key.

    Idempotency requires BOTH source and external_id (a real ingestion
    key). Two source-less missions that merely share an external_id are
    distinct and must both be created — and the create path must not
    500 (no MultipleResultsFound from the pre-check).
    """
    r1 = client.post("/api/v1/missions", json=_create_payload(
        external_id="issue-7", branch_prefix="feature/soy-a",
    ))
    assert r1.status_code == 201
    r2 = client.post("/api/v1/missions", json=_create_payload(
        external_id="issue-7", branch_prefix="feature/soy-b",
    ))
    assert r2.status_code == 201
    assert r2.json()["id"] != r1.json()["id"]  # not collapsed
    # A third with the same source-less external_id must still 201
    # (the pre-check never runs scalar_one_or_none on a multi-row set).
    r3 = client.post("/api/v1/missions", json=_create_payload(
        external_id="issue-7", branch_prefix="feature/soy-c",
    ))
    assert r3.status_code == 201
    assert client.get("/api/v1/missions").json()["total"] == 3


# ---------------------------------------------------------------------------
# 422 detail body shape — additional contract check
# ---------------------------------------------------------------------------
def test_422_payload_shape(client):
    r = client.post("/api/v1/missions", json={})
    assert r.status_code == 422
    body = r.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)
    for entry in body["detail"]:
        # Each Pydantic error has ``loc``, ``msg``, ``type`` (and
        # optionally ``input``). Confirm the structure.
        assert "loc" in entry
        assert "msg" in entry
        assert "type" in entry
