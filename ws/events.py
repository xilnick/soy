"""
soy.ws.events
=============

Real-time event bus for ASF.

The WebSocket layer is the canonical "fan-out" surface for
agent / task / mission lifecycle events. Producers
(:class:`soy.services.praisonai_worker.ASFWorker`) call
:func:`publish` from any thread; the bus hands the event to every
WebSocket client connected to the relevant mission.

This module is intentionally thin: it does not own the database,
it does not retry failed publishes, and it does not authenticate
WebSocket clients. The FastAPI router in
:mod:`soy.api.v1.websocket` (added in a later milestone) wires
the endpoint up; here we just provide the in-memory registry and
the publish helper.

Threading model
---------------

* Each connected client owns a *bounded* ``asyncio.Queue`` that the
  publisher writes to.
* :func:`publish` is called from non-asyncio threads (the ASF worker
  runs PraisonAI inside a thread-pool executor, and FastAPI's sync
  routes run in the anyio threadpool). ``asyncio.Queue`` is **not**
  thread-safe, so the publisher schedules the enqueue onto the
  client's own event loop via ``loop.call_soon_threadsafe`` — the
  loop is captured when the client registers. When no loop is bound
  (e.g. a unit test that registers and drains on the same thread with
  no running loop) the enqueue happens inline.
* The per-client queue is bounded: when a slow/stalled consumer fills
  it, the oldest event is dropped so a dead client cannot grow memory
  without bound or starve other subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("soy.ws.events")

# Per-client queue bound. A subscriber this far behind is treated as
# slow/dead: the oldest event is dropped to make room for the newest.
_QUEUE_MAXSIZE = 1000


@dataclass
class _Client:
    """A single WebSocket subscriber.

    The ``mission_id`` is either a UUID string for a per-mission
    subscription or the special token ``"*"`` for a global
    subscriber (e.g. a Mission Control dashboard). The ``queue``
    is filled by the publisher and drained by the WebSocket
    coroutine. ``loop`` is the event loop serving this client's
    socket, captured at registration; the publisher uses it to hand
    events across threads safely (``None`` when no loop is running,
    e.g. a same-thread unit test).
    """

    client_id: str
    mission_id: str  # UUID string, or "*" for global
    queue: "asyncio.Queue[Dict[str, Any]]" = field(
        default_factory=lambda: asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    )
    connected: bool = True
    loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Global registry
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_clients: Dict[str, _Client] = {}
_by_mission: Dict[str, Set[str]] = {}


def _register(client: _Client) -> None:
    with _lock:
        _clients[client.client_id] = client
        _by_mission.setdefault(client.mission_id, set()).add(client.client_id)


def _unregister(client_id: str) -> None:
    with _lock:
        client = _clients.pop(client_id, None)
        if client is None:
            return
        members = _by_mission.get(client.mission_id)
        if members is not None:
            members.discard(client_id)
            if not members:
                _by_mission.pop(client.mission_id, None)


def register(mission_id: str) -> _Client:
    """Create a new client subscribed to ``mission_id``.

    ``mission_id`` may be a UUID string or ``"*"`` for a global
    subscriber. The returned client owns an asyncio queue and
    a unique client id; the WebSocket coroutine drains the
    queue and forwards events to the browser.

    The running event loop is captured so the publisher can enqueue
    across threads safely; if no loop is running (a same-thread unit
    test), ``loop`` stays ``None`` and the publisher enqueues inline.
    """
    try:
        loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    client = _Client(
        client_id=str(uuid.uuid4()),
        mission_id=str(mission_id),
        loop=loop,
    )
    _register(client)
    return client


def unregister(client_id: str) -> None:
    """Disconnect a client by id. Safe to call multiple times."""
    _unregister(client_id)


def list_clients(mission_id: Optional[str] = None) -> List[str]:
    """Return the list of connected client ids (debug helper)."""
    with _lock:
        if mission_id is None:
            return list(_clients.keys())
        return list(_by_mission.get(str(mission_id), set()))


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def publish(event: str, payload: Dict[str, Any]) -> int:
    """Publish an event to every subscriber.

    Returns the number of clients the event was delivered to. The
    publisher never raises — broadcast failures are logged and
    swallowed so the worker can keep moving.
    """
    envelope: Dict[str, Any] = {
        "type": event,
        "payload": payload,
        "timestamp": _utc_iso(),
    }
    delivered = 0
    with _lock:
        targets: List[_Client] = []
        for client in _clients.values():
            if not client.connected:
                continue
            if client.mission_id == "*" or client.mission_id == str(
                payload.get("mission_id", "")
            ):
                targets.append(client)
    for client in targets:
        try:
            if client.loop is not None and not client.loop.is_closed():
                # Cross-thread handoff: mutate the asyncio.Queue only
                # on the loop thread that owns it.
                client.loop.call_soon_threadsafe(_enqueue, client, envelope)
            else:
                # No bound loop (same-thread unit test) — enqueue inline.
                _enqueue(client, envelope)
            delivered += 1
        except RuntimeError:
            # Loop was closed between the check and the call — the
            # client is gone; drop the event for it.
            logger.debug(
                "WebSocket client %s loop closed; dropping event %s",
                client.client_id, event,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to deliver event %s to %s: %s",
                event, client.client_id, exc,
            )
    return delivered


def _enqueue(client: _Client, envelope: Dict[str, Any]) -> None:
    """Put ``envelope`` on ``client.queue``, dropping the oldest on overflow.

    Runs on the client's event-loop thread (scheduled via
    ``call_soon_threadsafe``) or inline when no loop is bound. A
    bounded queue means a slow/dead consumer cannot grow memory
    without limit; the oldest event is evicted to make room.
    """
    try:
        client.queue.put_nowait(envelope)
    except asyncio.QueueFull:
        try:
            client.queue.get_nowait()  # drop oldest
        except Exception:  # noqa: BLE001
            pass
        try:
            client.queue.put_nowait(envelope)
        except Exception:  # noqa: BLE001
            logger.warning(
                "WebSocket client %s queue still full; event dropped",
                client.client_id,
            )


def _utc_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# WebSocket coroutine helpers
# ---------------------------------------------------------------------------
async def drain(client: _Client) -> Dict[str, Any]:
    """Wait for the next event on ``client.queue``.

    The coroutine blocks until an event is available. The
    WebSocket endpoint calls this in a loop, serialising each
    event as JSON and writing it to the socket.
    """
    return await client.queue.get()


def encode_event(envelope: Dict[str, Any]) -> str:
    """Serialise an envelope to JSON for the WebSocket frame."""
    return json.dumps(envelope, default=str)


# ---------------------------------------------------------------------------
# Worker publisher
# ---------------------------------------------------------------------------
def install_as_publisher() -> None:
    """Install :func:`publish` as the ASF worker's event publisher.

    Idempotent: calling it twice has the same effect as calling
    it once. The worker module looks up the singleton publisher
    so the wiring is one-line at startup.
    """
    from soy.services.praisonai_worker import set_event_publisher

    set_event_publisher(publish)


__all__ = [
    "drain",
    "encode_event",
    "install_as_publisher",
    "list_clients",
    "publish",
    "register",
    "unregister",
]
