"""
soy.api.v1.router
=================

Aggregates the v1 API routers.

This module exists so :mod:`soy.main` has a single import to wire
into the FastAPI app. New v1 routers (agents, tasks, executions,
approvals, chat, webhooks) are added here in their own milestones.
"""

from __future__ import annotations

from fastapi import APIRouter

from soy.api.v1 import (
    agents,
    executions,
    logs,
    missions,
    tasks,
    webhooks,
    websocket,
)

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(missions.router)
api_v1_router.include_router(agents.router)
api_v1_router.include_router(tasks.router)
api_v1_router.include_router(executions.router)
api_v1_router.include_router(logs.router)
api_v1_router.include_router(webhooks.router)

# WebSocket endpoint — registered directly on the app (not on
# the v1 router) because FastAPI's WebSocket support does not
# compose well with APIRouter prefixes; the path is canonical
# and must be ``/ws/...`` rather than ``/api/v1/ws/...``.
ws_router = websocket.router

__all__ = ["api_v1_router", "ws_router"]
