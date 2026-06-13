"""
Unit tests for the optional external integrations:

* Mission Control sync (asf.services.mission_control_sync)
* DeerFlow client (asf.services.deerflow_client)

Each integration is gated off by default; the tests assert both the
enabled behaviour AND that the disabled path is a genuine no-op (never
constructs a client / touches the network).
"""

from __future__ import annotations

import types
import uuid

import pytest


def _agent():
    return types.SimpleNamespace(
        id=uuid.uuid4(), mission_id=uuid.uuid4(), name="coder-1",
        role="coder", status="idle", model="ollama/x",
    )


def _task():
    return types.SimpleNamespace(
        id=uuid.uuid4(), mission_id=uuid.uuid4(), agent_id=uuid.uuid4(),
        description="do the thing", status="pending", attempt_count=0,
    )


def _mission():
    return types.SimpleNamespace(
        id=uuid.uuid4(), title="M", status="created", branch=None,
        repo_url="https://github.com/x/y",
    )


# ---------------------------------------------------------------------------
# Mission Control sync
# ---------------------------------------------------------------------------
def _record_posts(monkeypatch):
    from asf.services import mission_control_sync as mc
    calls = []
    monkeypatch.setattr(
        mc.MissionControlSync, "_post",
        lambda self, path, payload: calls.append((path, payload)) or True,
    )
    return calls


def test_mc_sync_disabled_is_noop(monkeypatch):
    from asf.services import mission_control_sync as mc
    monkeypatch.delenv("ASF_MC_SYNC_ENABLED", raising=False)
    calls = _record_posts(monkeypatch)
    mc.sync_agent(_agent())
    mc.sync_task(_task())
    mc.sync_mission_status(_mission())
    assert calls == []  # genuine no-op when the flag is off


def test_mc_sync_enabled_pushes_each_entity(monkeypatch):
    from asf.services import mission_control_sync as mc
    monkeypatch.setenv("ASF_MC_SYNC_ENABLED", "true")
    calls = _record_posts(monkeypatch)

    a = _agent()
    mc.sync_agent(a)
    mc.sync_task(_task())
    mc.sync_mission_status(_mission())

    paths = [p for p, _ in calls]
    assert paths == ["/api/agents", "/api/tasks", "/api/status"]
    agent_payload = calls[0][1]
    assert agent_payload["name"] == "coder-1"
    assert agent_payload["role"] == "coder"
    assert agent_payload["id"] == str(a.id)


def test_mc_post_swallows_errors_and_returns_false(monkeypatch):
    from asf.services import mission_control_sync as mc
    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise RuntimeError("MC is down")

    monkeypatch.setattr(httpx, "Client", _Boom)
    # Must not raise; returns False.
    assert mc.MissionControlSync(base_url="http://x")._post("/api/agents", {}) is False


def test_mc_headers_include_api_key(monkeypatch):
    from asf.services import mission_control_sync as mc
    client = mc.MissionControlSync(base_url="http://x", api_key="k-123")
    headers = client._headers()
    assert headers["Authorization"] == "Bearer k-123"
    assert headers["X-API-Key"] == "k-123"


# ---------------------------------------------------------------------------
# DeerFlow client
# ---------------------------------------------------------------------------
def test_deerflow_disabled_is_noop(monkeypatch):
    from asf.services import deerflow_client as dc
    monkeypatch.delenv("ASF_DEERFLOW_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(
        dc.DeerFlowClient, "trigger_sandbox_task",
        lambda self, **k: called.append(k),
    )
    assert dc.maybe_trigger_sandbox(
        task_id="t", description="d", sandbox=True,
    ) is None
    assert called == []  # no client constructed / no network


def test_deerflow_enabled_triggers_for_sandboxed_agent(monkeypatch):
    from asf.services import deerflow_client as dc
    monkeypatch.setenv("ASF_DEERFLOW_ENABLED", "true")
    called = []
    monkeypatch.setattr(
        dc.DeerFlowClient, "trigger_sandbox_task",
        lambda self, **k: (called.append(k) or {"run_id": "r1"}),
    )
    out = dc.maybe_trigger_sandbox(
        task_id="t1", description="do", sandbox=True, tools=["file_read"],
    )
    assert out == {"run_id": "r1"}
    assert called[0]["task_id"] == "t1"
    assert called[0]["tools"] == ["file_read"]


def test_deerflow_skips_non_sandboxed_agent(monkeypatch):
    from asf.services import deerflow_client as dc
    monkeypatch.setenv("ASF_DEERFLOW_ENABLED", "true")
    called = []
    monkeypatch.setattr(
        dc.DeerFlowClient, "trigger_sandbox_task",
        lambda self, **k: called.append(k),
    )
    # Enabled, but the agent is not sandboxed → no trigger.
    assert dc.maybe_trigger_sandbox(
        task_id="t", description="d", sandbox=False,
    ) is None
    assert called == []


def test_deerflow_trigger_swallows_errors(monkeypatch):
    from asf.services import deerflow_client as dc
    import httpx

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            raise RuntimeError("deerflow down")

    monkeypatch.setattr(httpx, "Client", _Boom)
    out = dc.DeerFlowClient(base_url="http://x").trigger_sandbox_task(
        task_id="t", description="d",
    )
    assert out is None  # never raises
