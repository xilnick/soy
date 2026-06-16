"""
soy.api.v1.websocket
====================

FastAPI WebSocket endpoint for real-time mission events.

The endpoint is mounted at ``/ws/missions/{mission_id}/events``.
Clients connect with a normal WebSocket handshake; the server
pushes JSON events as the ASF worker publishes them.

The actual broadcast logic lives in :mod:`soy.ws.events`. This
module is the FastAPI glue: it registers the client, drains the
event queue, and writes frames to the socket until the client
disconnects (or the server shuts down).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import uuid

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session, sessionmaker

from soy.db import get_session_factory
from soy.models.mission import Mission
from soy.ws import events as ws_events

logger = logging.getLogger("soy.api.v1.websocket")

router = APIRouter()


@router.websocket("/ws/missions/{mission_id}/events")
async def mission_events(
    websocket: WebSocket,
    mission_id: str,
    session_factory: sessionmaker[Session] = Depends(get_session_factory),
) -> None:
    """Stream mission events over a WebSocket connection.

    The mission_id path parameter may be a UUID or the wildcard
    ``"*"`` (a global subscriber such as a Mission Control
    dashboard). When the path is a UUID, the server validates
    that the mission exists; otherwise the handshake is closed
    with code 1008 (Policy Violation) per the validation
    contract.
    """
    # Accept the handshake first — Starlette requires
    # ``accept()`` before ``close()`` is callable. The
    # validation happens *after* the upgrade so the
    # ``close(1008)`` frame reaches the client.
    await websocket.accept()
    if mission_id == "*":
        # Global firehose (e.g. the Mission Control service). It
        # receives EVERY mission's events, so it is privileged: a
        # configured admin token must be presented on the handshake.
        # Default-deny — with no token configured the firehose is off.
        if not _wildcard_authorized(websocket):
            await websocket.close(
                code=1008, reason="wildcard_subscription_unauthorized",
            )
            return
        subscribe_id = "*"
    else:
        try:
            mid = uuid.UUID(mission_id)
        except ValueError:
            await websocket.close(code=1008, reason="invalid_mission_id")
            return
        # Confirm the mission exists. A missing mission is a
        # policy violation rather than a "not found" HTTP error
        # (the WebSocket is already past the HTTP layer). The
        # session is opened and released immediately so the
        # long-lived socket never pins a pooled DB connection.
        with session_factory() as db:
            exists = db.get(Mission, mid) is not None
        if not exists:
            await websocket.close(code=1008, reason="mission_not_found")
            return
        # Subscribe under the CANONICAL UUID string. Published
        # payloads always carry ``str(mission_id)`` (canonical
        # lowercase, dashed); registering the raw path form (e.g.
        # uppercase or dashless) would never match and the client
        # would silently receive zero events.
        subscribe_id = str(mid)

    client = ws_events.register(subscribe_id)
    # Actively receive on the socket in the background so a client
    # disconnect is observed promptly even while the mission is idle
    # (Starlette surfaces a close only through a receive or a send,
    # and a send happens only when an event is published). Without
    # this the handler would park forever in ``drain()`` and the
    # ``finally`` cleanup would never run, leaking the subscriber.
    recv_task = asyncio.create_task(_drain_inbound(websocket))
    # Track the current drain task outside the loop so the ``finally``
    # can cancel it too: if the handler is cancelled (e.g. server
    # shutdown) while parked in ``asyncio.wait``, ``CancelledError``
    # bypasses the except clauses, and a per-iteration drain task left
    # parked on ``queue.get()`` would otherwise leak.
    drain_task = None
    try:
        # Send a hello frame so the client knows the channel is
        # live and can synchronise its state.
        await websocket.send_json(
            {
                "type": "ws.hello",
                "payload": {
                    "client_id": client.client_id,
                    "mission_id": subscribe_id,
                },
                "timestamp": _utc_iso(),
            }
        )
        while True:
            drain_task = asyncio.create_task(ws_events.drain(client))
            done, _pending = await asyncio.wait(
                {recv_task, drain_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if drain_task in done:
                envelope = drain_task.result()
                await websocket.send_text(ws_events.encode_event(envelope))
                if recv_task.done():
                    break  # client disconnected
            else:
                # The receive completed first → the client
                # disconnected (or sent EOF). The drain task is still
                # parked on an empty queue (no event consumed), so
                # cancelling it loses nothing.
                drain_task.cancel()
                break
    except WebSocketDisconnect:
        logger.info("WebSocket client %s disconnected", client.client_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "WebSocket client %s error: %s", client.client_id, exc,
        )
        try:
            await websocket.close(code=1011, reason="internal_error")
        except Exception:  # noqa: BLE001
            pass
    finally:
        if drain_task is not None and not drain_task.done():
            drain_task.cancel()
        recv_task.cancel()
        ws_events.unregister(client.client_id)


async def _drain_inbound(websocket: WebSocket) -> None:
    """Consume inbound frames until the client disconnects.

    The endpoint is send-only from the application's perspective, but
    it must actively receive so a disconnect is detected promptly.
    Inbound data frames are ignored; a ``websocket.disconnect``
    message (or a raised :class:`WebSocketDisconnect`) ends the task.
    """
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        # Any other receive error (e.g. Starlette raising after the
        # socket entered an unexpected state) is treated as a
        # disconnect. Catching it here means this background task never
        # completes with an unretrieved exception (which the main loop
        # never awaits the result of), avoiding a noisy
        # "Task exception was never retrieved" warning at GC.
        logger.debug("WebSocket inbound receive ended: %s", exc)
        return


def _wildcard_authorized(websocket: WebSocket) -> bool:
    """Return True when a ``*`` (global firehose) subscription is allowed.

    Requires the ``ASF_WS_ADMIN_TOKEN`` env var to be configured AND a
    matching ``token`` query parameter on the handshake. Default-deny:
    if no admin token is configured the firehose is disabled, so an
    unauthenticated caller can never subscribe to every mission's
    events. The comparison is constant-time.
    """
    admin_token = os.environ.get("ASF_WS_ADMIN_TOKEN", "")
    if not admin_token:
        return False
    provided = websocket.query_params.get("token", "")
    if not provided:
        return False
    # Compare as UTF-8 bytes: ``hmac.compare_digest`` raises TypeError
    # on str inputs containing non-ASCII characters, and ``provided``
    # is fully attacker-controlled (the ?token= query param). Bytes
    # comparison is constant-time and accepts any input.
    return hmac.compare_digest(
        provided.encode("utf-8"), admin_token.encode("utf-8"),
    )


def _utc_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
