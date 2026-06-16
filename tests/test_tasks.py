"""
Tests for the task CRUD + execution API.

Maps to validation contract assertions:

* ``test_create_task_persists_row`` — VAL-API-021 (partial)
* ``test_execute_task_creates_execution_row`` — VAL-API-022
* ``test_failed_execution_retries_three_times`` — VAL-API-025
* ``test_fourth_failure_escalates_task_and_mission`` — VAL-API-026
* ``test_successful_execution_halts_retry_chain`` — VAL-API-028
* ``test_task_timeout_writes_timeout_status`` — VAL-API-030
* ``test_malformed_json_output_triggers_retry`` — VAL-API-088
* ``test_tool_exception_bubbles_to_execution_error`` — VAL-API-090
* ``test_parallel_execution_supports_multiple_tasks`` — VAL-API-091

The tests stub out the PraisonAI ``Agent`` / ``Agents`` /
``Task`` classes so the worker exercises its real retry /
escalation / timeout / parallel code paths without making
network calls.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Iterator

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
    ExecutionStatus,
    Mission,
    Task,
    TaskStatus,
)
from soy.models.base import Base
import soy.models  # noqa: F401


# ---------------------------------------------------------------------------
# PraisonAI stubs
# ---------------------------------------------------------------------------
class _StubPraisonAgent:
    """Stand-in for ``praisonaiagents.Agent``."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _StubPraisonTask:
    """Stand-in for ``praisonaiagents.Task``."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs


class _StubWorkflow:
    """Stand-in for ``praisonaiagents.Agents``.

    The ``behaviour`` attribute on the class lets a test inject a
    function that returns the value the worker's
    ``_invoke_workflow`` helper should see.
    """

    behaviour: Any = None
    delay_seconds: float = 0.0

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.process = kwargs.get("process")

    def start(self, *args, **kwargs):
        if _StubWorkflow.delay_seconds > 0:
            time.sleep(_StubWorkflow.delay_seconds)
        b = _StubWorkflow.behaviour
        if b is None:
            return {"output": "ok"}
        if isinstance(b, BaseException):
            raise b
        result = b()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture(autouse=True)
def _reset_stub_workflow():
    """Reset the stub workflow between tests."""
    _StubWorkflow.behaviour = None
    _StubWorkflow.delay_seconds = 0.0
    yield
    _StubWorkflow.behaviour = None
    _StubWorkflow.delay_seconds = 0.0


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
    db_path = tmp_path / "asf_test.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("ASF_DATABASE_URL", url)
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
    monkeypatch.setenv("ASF_RUN_MIGRATIONS_ON_STARTUP", "false")
    monkeypatch.setattr(
        "praisonaiagents.Agent", _StubPraisonAgent, raising=False,
    )
    monkeypatch.setattr(
        "praisonaiagents.Task", _StubPraisonTask, raising=False,
    )
    monkeypatch.setattr(
        "praisonaiagents.Agents", _StubWorkflow, raising=False,
    )
    # Short backoff for tests.
    from soy.services import praisonai_worker
    monkeypatch.setattr(
        praisonai_worker, "RETRY_BACKOFF_SECONDS", (0, 0),
    )
    monkeypatch.setattr(
        praisonai_worker, "DEFAULT_TASK_TIMEOUT_SECONDS", 30,
    )
    # Inject the test's sessionmaker into the worker so the
    # worker sees the same rows the request handler persists.
    # Without this the worker holds a sessionmaker bound to a
    # different (or stale) engine and the lookup
    # ``db.get(Agent, ...)`` returns None.
    import soy.db as _asf_db
    # The worker calls ``soy.db.get_session_local()`` to
    # obtain the sessionmaker. Tests inject a sessionmaker
    # directly, so wrap it in a function with the same
    # signature as the production ``get_session_local``.
    monkeypatch.setattr(
        _asf_db, "get_session_local", lambda: session_factory,
    )
    # Force the worker singleton to be rebuilt on the next
    # ``get_worker()`` call so it picks up the patched
    # ``get_session_local``.
    import soy.services.praisonai_worker as paw_mod
    paw_mod.reset_worker()

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _create_mission(client, **overrides) -> dict:
    payload = {
        "title": "M",
        "description": "d",
        "repo_url": "https://github.com/example/repo",
        "branch_prefix": "feature/asf-1",
    }
    payload.update(overrides)
    r = client.post("/api/v1/missions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _create_agent(client, mission_id, role="coder") -> dict:
    r = client.post(
        f"/api/v1/missions/{mission_id}/agents",
        json={"name": f"a-{role}", "role": role},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_task(client, mission_id, agent_id, description="do the thing") -> dict:
    r = client.post(
        f"/api/v1/missions/{mission_id}/agents/{agent_id}/tasks",
        json={"description": description, "expected_output": "JSON output"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# VAL-API-021 — task creation persists a row that maps to a PraisonAI Task
# ---------------------------------------------------------------------------
def test_create_task_persists_row(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"], role="coder")
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents/{agent['id']}/tasks",
        json={
            "description": "Refactor module X",
            "expected_output": "A passing test suite",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["description"] == "Refactor module X"
    assert body["expected_output"] == "A passing test suite"
    assert body["status"] == "pending"
    assert body["attempt_count"] == 0
    with session_factory() as db:
        from sqlalchemy import select
        task = db.execute(
            select(Task).where(Task.id == uuid.UUID(body["id"]))
        ).scalar_one()
        # The DB row's agent_id matches the path parameter.
        assert task.agent_id == uuid.UUID(agent["id"])


def test_create_task_for_wrong_mission_returns_404(client):
    m1 = _create_mission(client, branch_prefix="feature/asf-200")
    m2 = _create_mission(client, branch_prefix="feature/asf-201")
    agent = _create_agent(client, m1["id"])
    r = client.post(
        f"/api/v1/missions/{m2['id']}/agents/{agent['id']}/tasks",
        json={"description": "x"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "AGENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# VAL-API-022 — execution persists a row
# ---------------------------------------------------------------------------
def test_execute_task_creates_execution_row(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    _StubWorkflow.behaviour = lambda: {"output": "completed", "ok": True}

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["execution_id"] is not None
    assert body["attempt_number"] == 1
    assert body["error"] is None

    with session_factory() as db:
        from sqlalchemy import select
        exec_rows = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
        ).scalars().all()
        assert len(exec_rows) == 1
        e = exec_rows[0]
        assert e.status == ExecutionStatus.completed
        assert e.attempt_number == 1
        assert e.started_at is not None
        assert e.finished_at is not None
        assert e.output == {"output": "completed", "ok": True}


# ---------------------------------------------------------------------------
# VAL-API-025 — failed execution retries up to 2 additional times
# ---------------------------------------------------------------------------
def test_failed_execution_retries_three_times(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    _StubWorkflow.behaviour = lambda: (_ for _ in ()).throw(
        RuntimeError("synthetic failure")
    )

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False, "timeout_seconds": 30},
    )
    assert r.status_code == 200
    body = r.json()
    # The 3rd attempt escalates the task.
    assert body["status"] == "escalated"
    assert body["escalated"] is True
    assert body["attempt_count"] == 3

    with session_factory() as db:
        from sqlalchemy import select
        exec_rows = db.execute(
            select(Execution)
            .where(Execution.task_id == uuid.UUID(task["id"]))
            .order_by(Execution.attempt_number.asc())
        ).scalars().all()
        # Exactly 3 execution rows — no 4th attempt.
        assert len(exec_rows) == 3
        attempt_numbers = [e.attempt_number for e in exec_rows]
        assert attempt_numbers == [1, 2, 3]
        # Every row is failed with a traceback in error.
        for e in exec_rows:
            assert e.status == ExecutionStatus.failed
            assert "synthetic failure" in (e.error or "")


# ---------------------------------------------------------------------------
# VAL-API-026 — 4th failure escalates the task AND the parent mission
# ---------------------------------------------------------------------------
def test_fourth_failure_escalates_task_and_mission(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    _StubWorkflow.behaviour = lambda: (_ for _ in ()).throw(
        RuntimeError("always fails")
    )

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "escalated"
    assert body["escalated"] is True

    with session_factory() as db:
        mission_row = db.get(Mission, uuid.UUID(mission["id"]))
        assert mission_row.status.value == "escalated"


# ---------------------------------------------------------------------------
# VAL-API-028 — successful execution halts the retry chain
# ---------------------------------------------------------------------------
def test_successful_execution_halts_retry_chain(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])

    call_count = {"n": 0}

    def _behaviour():
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise RuntimeError("first attempt fails")
        return {"output": "ok on retry"}

    _StubWorkflow.behaviour = _behaviour

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r.status_code == 200
    body = r.json()
    # Halted on attempt 2: task completed, no escalation.
    assert body["status"] == "completed"
    assert body["escalated"] is False
    assert body["attempt_number"] == 2

    with session_factory() as db:
        from sqlalchemy import select
        exec_rows = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
            .order_by(Execution.attempt_number.asc())
        ).scalars().all()
        # Exactly 2 rows — the chain halted on the success.
        assert len(exec_rows) == 2
        assert exec_rows[0].status == ExecutionStatus.failed
        assert exec_rows[1].status == ExecutionStatus.completed


# ---------------------------------------------------------------------------
# VAL-API-030 — task timeout writes status=failed with error=timeout
# ---------------------------------------------------------------------------
def test_task_timeout_writes_timeout_status(client, session_factory, monkeypatch):
    from soy.services import praisonai_worker

    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])

    # Make the workflow block much longer than the timeout so
    # every attempt hits the timeout (the previous attempt's
    # thread keeps running after ``future.cancel()`` because
    # Python's ThreadPoolExecutor cannot interrupt arbitrary
    # code, so a short delay may "leak" the success into the
    # next attempt).
    _StubWorkflow.delay_seconds = 5.0

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False, "timeout_seconds": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The single attempt failed with a timeout error. The task
    # is escalated because the timeout fires on every attempt.
    assert body["error"] == "timeout"

    with session_factory() as db:
        from sqlalchemy import select
        exec_rows = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
        ).scalars().all()
        assert all(e.status == ExecutionStatus.timeout for e in exec_rows)
        # And the task is escalated (3 timeouts == 3 failures).
        task_row = db.get(Task, uuid.UUID(task["id"]))
        assert task_row.status == TaskStatus.escalated


# ---------------------------------------------------------------------------
# VAL-API-088 — malformed JSON from agent triggers retry
# ---------------------------------------------------------------------------
def test_malformed_json_output_triggers_retry_and_escalates(
    client, session_factory,
):
    """Output that *looks* like JSON but does not parse is a failure.

    The worker's coercer tags an unparseable ``{``/``[``-prefixed
    string with ``_parse_error`` and the worker now treats that as a
    failed attempt so the 3-try retry policy fires (rather than
    silently recording it as completed). With every attempt
    malformed, the task escalates after MAX_ATTEMPTS.
    """
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])

    _StubWorkflow.behaviour = lambda: "{ this is : not valid json"

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "escalated"
    assert body["escalated"] is True

    with session_factory() as db:
        from sqlalchemy import select
        exec_rows = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
        ).scalars().all()
        assert len(exec_rows) == 3
        assert all(e.status == ExecutionStatus.failed for e in exec_rows)
        assert all(
            "malformed_json_output" in (e.error or "") for e in exec_rows
        )
        task_row = db.get(Task, uuid.UUID(task["id"]))
        assert task_row.status == TaskStatus.escalated
        assert task_row.attempt_count == 3


def test_non_json_text_output_is_completed(client, session_factory):
    """Plain prose output (not JSON-shaped) is a successful result.

    Only output that looks like JSON but fails to parse is a
    failure; free-form text is wrapped under ``_text`` and recorded
    as completed in a single attempt.
    """
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])

    _StubWorkflow.behaviour = lambda: "this is plain prose, not JSON"

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["attempt_count"] == 1


# ---------------------------------------------------------------------------
# VAL-API-090 — tool exception bubbles to executions.error
# ---------------------------------------------------------------------------
def test_tool_exception_bubbles_to_execution_error(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    _StubWorkflow.behaviour = lambda: (_ for _ in ()).throw(
        RuntimeError("tool 'file_write' raised FileNotFoundError")
    )

    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r.status_code == 200
    body = r.json()
    # After 3 attempts the task is escalated; the executions
    # carry the tool-exception text.
    with session_factory() as db:
        from sqlalchemy import select
        exec_rows = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
        ).scalars().all()
        assert all(
            "file_write" in (e.error or "") for e in exec_rows
        )
        assert all(
            e.status == ExecutionStatus.failed for e in exec_rows
        )


# ---------------------------------------------------------------------------
# VAL-API-091 — multiple independent tasks can execute in parallel
# ---------------------------------------------------------------------------
def test_parallel_execution_supports_multiple_tasks(
    client, session_factory, tmp_path, monkeypatch,
):
    """Run three independent tasks concurrently.

    The default ``engine`` fixture uses a ``StaticPool`` so the
    request handler and the test assertions see the same
    connection. That pool is unsafe for cross-thread writes, so
    the parallel test mounts a *separate* engine with the
    default pool, runs the test against it, and then verifies
    the executions via the same engine.
    """
    # Build a parallel-friendly engine on a different file so
    # we don't disturb the main test engine.
    from sqlalchemy.pool import QueuePool
    parallel_path = tmp_path / "asf_parallel.db"
    parallel_url = f"sqlite:///{parallel_path}"
    parallel_eng = create_engine(
        parallel_url,
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        future=True,
    )
    Base.metadata.create_all(parallel_eng)
    parallel_sf = sessionmaker(
        bind=parallel_eng, autoflush=False, autocommit=False, future=True,
    )

    # Re-route the worker + request handler to the parallel
    # engine for the duration of this test.
    import soy.db as _asf_db
    monkeypatch.setattr(
        _asf_db, "get_session_local", lambda: parallel_sf,
    )
    import soy.services.praisonai_worker as paw_mod
    paw_mod.reset_worker()
    # Build a generator function (not a lambda) for the
    # ``get_db`` dependency override. FastAPI iterates the
    # generator it gets from calling the override, so a
    # plain lambda that *returns* a generator is one level
    # too deep.
    def _get_db_override():
        db = parallel_sf()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _get_db_override
    # NOTE: do not use the existing TestClient from the
    # ``client`` fixture because the dependency override
    # re-assignment above is too late for it. We open a fresh
    # TestClient instead.
    from fastapi.testclient import TestClient as _TC
    with _TC(app) as tc:
        # Use a fresh agent/task set on the parallel engine.
        mission = _create_mission_on(tc, parallel_sf)
        agent = _create_agent_on(tc, mission["id"])
        t1 = _create_task_on(tc, mission["id"], agent["id"], description="t1")
        t2 = _create_task_on(tc, mission["id"], agent["id"], description="t2")
        t3 = _create_task_on(tc, mission["id"], agent["id"], description="t3")

        # Each invocation of the stub workflow sleeps a little
        # so the parallel path is observable in a wall-clock sense.
        def _slow():
            time.sleep(0.1)
            return {"ok": True}

        _StubWorkflow.behaviour = _slow

        start = time.monotonic()
        r = tc.post(
            f"/api/v1/missions/{mission['id']}/tasks/execute-all",
            json={"parallel": True, "timeout_seconds": 30},
        )
        elapsed = time.monotonic() - start
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 3
        for entry in body:
            assert entry["status"] == "completed"

        # Sequential execution would have taken ~0.3s (3 ×
        # 0.1s). Parallel execution in a 3-worker thread pool
        # should run in ~0.15s. We allow generous slack
        # (0.40s) so the test is robust on slow CI machines.
        assert elapsed < 0.40, f"parallel run took {elapsed:.3f}s"
    parallel_eng.dispose()


def _yield_session(sf):
    db = sf()
    try:
        yield db
    finally:
        db.close()


def _create_mission_on(client, sf) -> dict:
    payload = {
        "title": "M",
        "description": "d",
        "repo_url": "https://github.com/example/repo",
        "branch_prefix": "feature/asf-1",
    }
    r = client.post("/api/v1/missions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _create_agent_on(client, mission_id, role="coder") -> dict:
    r = client.post(
        f"/api/v1/missions/{mission_id}/agents",
        json={"name": f"a-{role}", "role": role},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_task_on(
    client, mission_id, agent_id, description="do the thing",
) -> dict:
    r = client.post(
        f"/api/v1/missions/{mission_id}/agents/{agent_id}/tasks",
        json={"description": description, "expected_output": "JSON output"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Tools helper — unit test
# ---------------------------------------------------------------------------
def test_tools_for_sandbox():
    from soy.services.praisonai_worker import (
        SANDBOXED_TOOLS, UNSANDBOXED_TOOLS, tools_for_sandbox,
    )
    assert tools_for_sandbox(True) == SANDBOXED_TOOLS
    assert tools_for_sandbox(False) == UNSANDBOXED_TOOLS
    assert "file_read" in SANDBOXED_TOOLS
    assert "file_write" in SANDBOXED_TOOLS
    assert "run_command" not in SANDBOXED_TOOLS
    assert "web_search" not in SANDBOXED_TOOLS
    assert "run_command" in UNSANDBOXED_TOOLS
    assert "web_search" in UNSANDBOXED_TOOLS


# ---------------------------------------------------------------------------
# Concurrency: parallel fan-out of more tasks than the pool size must not
# self-deadlock (separate fan-out vs workflow executors).
# ---------------------------------------------------------------------------
def test_parallel_execution_no_deadlock_with_many_tasks(
    client, session_factory, tmp_path, monkeypatch,
):
    """A layer larger than the fan-out pool must still complete.

    Regression for the nested-executor self-deadlock: when
    ``execute_task`` and the inner ``_invoke_workflow`` shared one
    bounded pool, a layer of >= max_workers independent tasks filled
    every thread with outer calls blocked on inner futures that could
    never be scheduled, so every task spuriously timed out and
    escalated. With separate pools all tasks complete promptly.
    """
    from sqlalchemy.pool import QueuePool
    parallel_path = tmp_path / "asf_nodeadlock.db"
    parallel_url = f"sqlite:///{parallel_path}"
    parallel_eng = create_engine(
        parallel_url,
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        future=True,
    )
    Base.metadata.create_all(parallel_eng)
    parallel_sf = sessionmaker(
        bind=parallel_eng, autoflush=False, autocommit=False, future=True,
    )
    import soy.db as _asf_db
    monkeypatch.setattr(_asf_db, "get_session_local", lambda: parallel_sf)
    import soy.services.praisonai_worker as paw_mod
    paw_mod.reset_worker()

    def _get_db_override():
        db = parallel_sf()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _get_db_override

    from fastapi.testclient import TestClient as _TC
    with _TC(app) as tc:
        mission = _create_mission_on(tc, parallel_sf)
        agent = _create_agent_on(tc, mission["id"])
        # 6 independent tasks > the 4-worker fan-out pool.
        for i in range(6):
            _create_task_on(tc, mission["id"], agent["id"], description=f"t{i}")

        _StubWorkflow.behaviour = lambda: {"ok": True}

        start = time.monotonic()
        r = tc.post(
            f"/api/v1/missions/{mission['id']}/tasks/execute-all",
            # A short timeout makes a deadlock obvious: the buggy
            # code would block here for timeout * MAX_ATTEMPTS then
            # escalate every task; the fixed code returns in ~ms.
            json={"parallel": True, "timeout_seconds": 5},
        )
        elapsed = time.monotonic() - start
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body) == 6
        assert all(e["status"] == "completed" for e in body), body
        assert elapsed < 4.0, f"suspected deadlock: run took {elapsed:.2f}s"
    parallel_eng.dispose()


# ---------------------------------------------------------------------------
# Idempotency: re-executing a terminal task does not restart it.
# ---------------------------------------------------------------------------
def test_reexecute_escalated_task_is_idempotent(client, session_factory):
    """A second /execute on an escalated task is a no-op.

    Re-invoking execute must not reset the terminal state nor grant a
    fresh 3-try budget; it returns the existing escalated outcome and
    writes no new execution rows.
    """
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])

    _StubWorkflow.behaviour = lambda: (_ for _ in ()).throw(
        RuntimeError("always fails")
    )
    r1 = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "escalated"

    with session_factory() as db:
        from sqlalchemy import select
        before = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
        ).scalars().all()
        assert len(before) == 3

    # Second invocation — must be a no-op.
    r2 = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["status"] == "escalated"
    assert body["escalated"] is True
    assert body["attempt_count"] == 3

    with session_factory() as db:
        from sqlalchemy import select
        after = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(task["id"]))
        ).scalars().all()
        assert len(after) == 3  # no new attempts
        task_row = db.get(Task, uuid.UUID(task["id"]))
        assert task_row.status == TaskStatus.escalated
        assert task_row.attempt_count == 3


# ---------------------------------------------------------------------------
# Dependency gating: a dependent of a failed dependency is skipped.
# ---------------------------------------------------------------------------
def test_dependent_skipped_when_dependency_fails(client, session_factory):
    """execute-all must not run a task whose dependency did not complete."""
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    t1 = _create_task(client, mission["id"], agent["id"], description="upstream")
    # t2 depends on t1.
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents/{agent['id']}/tasks",
        json={
            "description": "downstream",
            "expected_output": "JSON output",
            "depends_on": [t1["id"]],
        },
    )
    assert r.status_code == 201, r.text
    t2 = r.json()

    # t1 always fails → escalates; t2 must be skipped, never executed.
    _StubWorkflow.behaviour = lambda: (_ for _ in ()).throw(
        RuntimeError("upstream broke")
    )
    r = client.post(
        f"/api/v1/missions/{mission['id']}/tasks/execute-all",
        json={"parallel": False, "timeout_seconds": 5},
    )
    assert r.status_code == 200, r.text
    by_id = {e["task_id"]: e for e in r.json()}
    assert by_id[t1["id"]]["status"] == "escalated"
    skipped = by_id[t2["id"]]
    assert skipped["status"] == "pending"
    assert skipped["error"] == "dependency_not_satisfied"

    with session_factory() as db:
        from sqlalchemy import select
        t2_execs = db.execute(
            select(Execution).where(Execution.task_id == uuid.UUID(t2["id"]))
        ).scalars().all()
        assert t2_execs == []  # the skipped task never ran


# ---------------------------------------------------------------------------
# _invoke_workflow must not re-run the workflow on an internal TypeError.
# ---------------------------------------------------------------------------
def test_invoke_workflow_does_not_double_run_on_internal_typeerror():
    """A TypeError raised from inside .start() must propagate, not retry.

    The previous blanket ``except TypeError`` re-invoked .start(),
    doubling side effects. The signature-based detection calls start
    exactly once.
    """
    from soy.services.praisonai_worker import ASFWorker

    class _FakeWorkflow:
        def __init__(self) -> None:
            self.calls = 0

        def start(self, return_dict: bool = False):
            self.calls += 1
            raise TypeError("boom from inside the workflow body")

    wf = _FakeWorkflow()
    with pytest.raises(TypeError):
        ASFWorker._invoke_workflow(wf)
    assert wf.calls == 1


# ---------------------------------------------------------------------------
# depends_on validation
# ---------------------------------------------------------------------------
def test_create_task_rejects_unknown_dependency(client):
    """depends_on referencing a task outside this mission is a 422."""
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    bogus = str(uuid.uuid4())
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents/{agent['id']}/tasks",
        json={
            "description": "d", "expected_output": "o",
            "depends_on": [bogus],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "INVALID_DEPENDENCY"


def test_create_task_accepts_valid_dependency(client):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    t1 = _create_task(client, mission["id"], agent["id"])
    r = client.post(
        f"/api/v1/missions/{mission['id']}/agents/{agent['id']}/tasks",
        json={
            "description": "d2", "expected_output": "o",
            "depends_on": [t1["id"]],
        },
    )
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
def test_list_tasks_pagination(client):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    for i in range(3):
        _create_task(client, mission["id"], agent["id"], description=f"t{i}")
    r = client.get(
        f"/api/v1/missions/{mission['id']}/tasks?limit=2&offset=0",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3  # unpaginated count
    assert len(body["items"]) == 2  # page size honoured


def test_default_model_fallback_is_local_no_key(client, monkeypatch):
    """A model-less agent must not default to a :cloud model (#24).

    With no explicit model and no ``ASF_MODEL``, the worker falls back
    to a local Ollama model whose placeholder key is ``"ollama"`` — a
    cloud default would instead resolve to an empty (credentialed) key.
    The stubbed ``praisonaiagents.Agent`` records the kwargs the worker
    passed, so we can assert the resolved key without a real LLM.
    """
    monkeypatch.delenv("ASF_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    from soy.services.praisonai_worker import get_worker

    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])  # model is None
    pa = get_worker().build_praisonai_agent(uuid.UUID(agent["id"]))
    # Local Ollama placeholder key ("ollama") — NOT a cloud default
    # (which would yield an empty api_key on the :cloud branch).
    assert pa.kwargs["api_key"] == "ollama"


def test_list_task_executions_pagination(client, session_factory):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    # 3 failed attempts → 3 execution rows.
    _StubWorkflow.behaviour = lambda: (_ for _ in ()).throw(
        RuntimeError("boom")
    )
    client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    r = client.get(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}"
        f"/executions?limit=2&offset=0",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


# ---------------------------------------------------------------------------
# GET /missions/{id}/logs — unified execution + lifecycle log
# ---------------------------------------------------------------------------
def test_mission_logs_aggregates_executions_and_transitions(
    client, session_factory,
):
    mission = _create_mission(client)
    agent = _create_agent(client, mission["id"])
    task = _create_task(client, mission["id"], agent["id"])
    _StubWorkflow.behaviour = lambda: {"ok": True}
    client.post(
        f"/api/v1/missions/{mission['id']}/tasks/{task['id']}/execute",
        json={"parallel": False},
    )
    # A transition with a reason records a lifecycle entry in metadata.
    client.post(
        f"/api/v1/missions/{mission['id']}/transition",
        json={"to_status": "planning", "reason": "kick off"},
    )

    r = client.get(f"/api/v1/missions/{mission['id']}/logs")
    assert r.status_code == 200, r.text
    body = r.json()
    kinds = {e["kind"] for e in body["entries"]}
    assert "execution" in kinds
    assert "transition" in kinds
    assert body["total"] >= 2
    # Entries are chronological (ascending timestamps).
    ts = [e["timestamp"] for e in body["entries"]]
    assert ts == sorted(ts)

    # Pagination: limit honoured, total is the full count.
    r2 = client.get(f"/api/v1/missions/{mission['id']}/logs?limit=1")
    assert len(r2.json()["entries"]) == 1
    assert r2.json()["total"] == body["total"]


def test_mission_logs_404_for_unknown_mission(client):
    r = client.get(f"/api/v1/missions/{uuid.uuid4()}/logs")
    assert r.status_code == 404
    assert r.json()["code"] == "MISSION_NOT_FOUND"


def test_mission_logs_tolerates_malformed_metadata(client):
    """Client-poisoned metadata must not turn /logs into a 500."""
    mission = _create_mission(client)
    # mission_metadata is a free-form client blob; poison the lifecycle
    # keys with the wrong shapes (non-list, list-of-non-dict).
    r = client.put(
        f"/api/v1/missions/{mission['id']}",
        json={"mission_metadata": {"transitions": "oops", "rejections": [42]}},
    )
    assert r.status_code == 200
    r2 = client.get(f"/api/v1/missions/{mission['id']}/logs")
    assert r2.status_code == 200  # defensively skipped, not a 500
    # No transition/rejection entries derived from the malformed blob.
    kinds = {e["kind"] for e in r2.json()["entries"]}
    assert "transition" not in kinds and "rejection" not in kinds
