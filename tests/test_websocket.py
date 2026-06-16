"""
Tests for the ASF WebSocket event bus.

Covers:

* Client registration / unregistration.
* Broadcast to a single-mission subscriber.
* Broadcast to a global (``*``) subscriber.
* Multiple subscribers receive the same envelope.
* Invalid mission_id triggers close code 1008.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from soy.db import get_db, get_session_factory
from soy.main import app
from soy.models import Mission
from soy.models.base import Base
import soy.models  # noqa: F401


@pytest.fixture
def engine(tmp_path, monkeypatch):
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
    # The WebSocket handler resolves its DB session via the
    # ``get_session_factory`` dependency, not ``get_db``. Override it
    # with the test's session factory so the existence check runs
    # against the same engine that created the mission.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    monkeypatch.setenv("ASF_RUN_MIGRATIONS_ON_STARTUP", "false")
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
            "branch_prefix": "feature/asf-1",
        },
    )
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# Module-level (no TestClient needed)
# ---------------------------------------------------------------------------
def test_publish_routes_to_matching_mission():
    from soy.ws import events

    events.unregister("")  # in case the previous test left a client
    client = events.register("mission-x")
    try:
        delivered = events.publish("mission.created", {"mission_id": "mission-x"})
        assert delivered == 1
        # The event landed in the queue.
        envelope = client.queue.get_nowait()
        assert envelope["type"] == "mission.created"
        assert envelope["payload"]["mission_id"] == "mission-x"
    finally:
        events.unregister(client.client_id)


def test_publish_does_not_route_to_other_mission():
    from soy.ws import events

    client = events.register("mission-y")
    try:
        delivered = events.publish("mission.created", {"mission_id": "mission-z"})
        assert delivered == 0
        # The queue is empty.
        with pytest.raises(asyncio.QueueEmpty):
            client.queue.get_nowait()
    finally:
        events.unregister(client.client_id)


def test_publish_routes_to_global_subscriber():
    from soy.ws import events

    global_client = events.register("*")
    try:
        delivered = events.publish("mission.created", {"mission_id": "anything"})
        assert delivered == 1
        envelope = global_client.queue.get_nowait()
        assert envelope["type"] == "mission.created"
    finally:
        events.unregister(global_client.client_id)


def test_publish_broadcasts_to_multiple_subscribers():
    from soy.ws import events

    a = events.register("mission-a")
    b = events.register("mission-a")
    try:
        delivered = events.publish("mission.created", {"mission_id": "mission-a"})
        assert delivered == 2
    finally:
        events.unregister(a.client_id)
        events.unregister(b.client_id)


# ---------------------------------------------------------------------------
# TestClient-driven: invalid mission_id triggers 1008 close
# ---------------------------------------------------------------------------
def test_websocket_rejects_unknown_mission_with_1008(client, session_factory):
    """Connecting to a non-existent mission id closes the WebSocket.

    The server validates the path parameter after the handshake
    and immediately closes with code 1008 (Policy Violation).
    A subsequent ``receive_*`` call surfaces the close as a
    :class:`WebSocketDisconnect` exception.
    """
    from starlette.websockets import WebSocketDisconnect
    missing = str(uuid.uuid4())
    with client.websocket_connect(f"/ws/missions/{missing}/events") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_websocket_rejects_invalid_mission_id_with_1008(client):
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect("/ws/missions/not-a-uuid/events") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_websocket_connects_and_receives_hello(client, session_factory):
    mission = _create_mission(client)
    with client.websocket_connect(
        f"/ws/missions/{mission['id']}/events"
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "ws.hello"
        assert hello["payload"]["mission_id"] == mission["id"]
        # Trigger an event; the subscriber should receive it.
        from soy.ws import events
        events.publish("mission.planning", {"mission_id": mission["id"]})
        envelope = ws.receive_json()
        assert envelope["type"] == "mission.planning"
        assert envelope["payload"]["mission_id"] == mission["id"]
        assert "timestamp" in envelope


# ---------------------------------------------------------------------------
# Cross-thread publish (the production threading model)
# ---------------------------------------------------------------------------
def test_cross_thread_publish_wakes_parked_drain():
    """publish() from a worker thread must wake a parked drain().

    Regression for the lost-wakeup defect: the publisher must hand the
    event to the client's event loop via ``call_soon_threadsafe``
    rather than mutating the (non-thread-safe) ``asyncio.Queue``
    directly from the foreign thread. With the old direct
    ``put_nowait`` the parked drain would not wake and this test would
    time out.
    """
    import threading
    from soy.ws import events

    captured: dict = {}

    async def _run():
        client = events.register("m-xthread")  # captures THIS loop
        try:
            drain_task = asyncio.create_task(events.drain(client))
            await asyncio.sleep(0.02)  # let drain park on queue.get()
            threading.Thread(
                target=lambda: events.publish(
                    "task.completed", {"mission_id": "m-xthread"},
                )
            ).start()
            captured["env"] = await asyncio.wait_for(drain_task, timeout=2.0)
        finally:
            events.unregister(client.client_id)

    asyncio.run(_run())
    assert captured["env"]["type"] == "task.completed"


def test_publish_bounded_queue_drops_oldest(monkeypatch):
    """A bounded per-client queue evicts the oldest event on overflow."""
    from soy.ws import events

    monkeypatch.setattr(events, "_QUEUE_MAXSIZE", 3)
    client = events.register("m-bound")  # no running loop → inline enqueue
    try:
        for i in range(5):
            events.publish("e", {"mission_id": "m-bound", "n": i})
        items = []
        while True:
            try:
                items.append(client.queue.get_nowait())
            except Exception:  # noqa: BLE001 — QueueEmpty terminates
                break
        # Queue capped at 3; oldest (n=0,1) dropped, newest retained.
        assert [it["payload"]["n"] for it in items] == [2, 3, 4]
    finally:
        events.unregister(client.client_id)


# ---------------------------------------------------------------------------
# Wildcard '*' firehose authorization
# ---------------------------------------------------------------------------
def test_wildcard_subscription_denied_without_token(client):
    """The '*' firehose is default-denied when no admin token is set."""
    from starlette.websockets import WebSocketDisconnect

    with client.websocket_connect("/ws/missions/*/events") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_wildcard_subscription_allowed_with_token(client, monkeypatch):
    """A matching admin token authorizes the '*' firehose."""
    monkeypatch.setenv("ASF_WS_ADMIN_TOKEN", "s3cret-token")
    with client.websocket_connect(
        "/ws/missions/*/events?token=s3cret-token"
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "ws.hello"
        assert hello["payload"]["mission_id"] == "*"
        # The firehose receives any mission's events.
        from soy.ws import events
        events.publish("mission.created", {"mission_id": "anything"})
        env = ws.receive_json()
        assert env["type"] == "mission.created"


# ---------------------------------------------------------------------------
# Non-canonical UUID path normalisation
# ---------------------------------------------------------------------------
def test_noncanonical_uuid_path_receives_events(client):
    """An upper-case UUID in the path still receives the mission's events.

    The handler registers under the canonical UUID string, so the
    publisher's ``str(mission_id)`` routing matches; the old code
    registered the raw path form and silently delivered nothing.
    """
    mission = _create_mission(client)
    upper = mission["id"].upper()
    with client.websocket_connect(f"/ws/missions/{upper}/events") as ws:
        hello = ws.receive_json()
        assert hello["payload"]["mission_id"] == mission["id"]  # canonical
        from soy.ws import events
        events.publish("mission.planning", {"mission_id": mission["id"]})
        env = ws.receive_json()
        assert env["type"] == "mission.planning"


def test_publish_routes_through_loop_when_bound():
    """Deterministic guard for the cross-thread handoff (#3/#8).

    When a client has a bound event loop, ``publish()`` must hand the
    enqueue to that loop via ``call_soon_threadsafe`` rather than
    mutating the asyncio.Queue from the calling thread. A revert to a
    direct ``put_nowait``/``_enqueue`` would leave ``call_soon_threadsafe``
    uncalled and fail this test (unlike the timing-dependent
    cross-thread test, this fails closed).
    """
    from soy.ws import events

    class _RecordingLoop:
        def __init__(self):
            self.calls = []

        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *a):
            self.calls.append(fn)
            fn(*a)  # run inline so the envelope still lands on the queue

    client = events.register("m-loop")
    client.loop = _RecordingLoop()
    try:
        delivered = events.publish("task.completed", {"mission_id": "m-loop"})
        assert delivered == 1
        # The enqueue was routed THROUGH the loop, not done directly.
        assert client.loop.calls == [events._enqueue]
        assert client.queue.get_nowait()["type"] == "task.completed"
    finally:
        events.unregister(client.client_id)


def test_idle_client_unregistered_on_disconnect(client):
    """An idle subscriber (no events published) is cleaned up on close.

    Guard for #2: the handler actively receives so a disconnect is
    detected even when the mission never publishes. The close is sent
    from the client WHILE the connection context is still open, then we
    poll for the subscriber to disappear — so cleanup must come from
    the handler's own disconnect-detection, NOT from the TestClient
    teardown (which cancels the handler on context-exit regardless and
    would mask a park-forever-in-drain() regression).
    """
    import time as _t
    from soy.ws import events

    mission = _create_mission(client)
    mid = mission["id"]
    cm = client.websocket_connect(f"/ws/missions/{mid}/events")
    ws = cm.__enter__()
    try:
        ws.receive_json()  # hello — registration has happened
        deadline = _t.time() + 2.0
        while _t.time() < deadline and not events.list_clients(mid):
            _t.sleep(0.02)
        assert len(events.list_clients(mid)) == 1

        # Client-initiated close, context still open.
        ws.close()
        deadline = _t.time() + 2.0
        while _t.time() < deadline and events.list_clients(mid):
            _t.sleep(0.02)
        assert events.list_clients(mid) == [], (
            "idle client was not unregistered on disconnect "
            "(handler not detecting the close)"
        )
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — already closed
            pass


def test_wildcard_nonascii_token_denied_cleanly(client, monkeypatch):
    """A non-ASCII ?token= is rejected cleanly (no unhandled TypeError)."""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("ASF_WS_ADMIN_TOKEN", "secret")
    with client.websocket_connect("/ws/missions/*/events?token=tøken") as ws:
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
