"""
asf.api.v1.logs

Unified, chronological mission log.

  * ``GET /api/v1/missions/{id}/logs`` — execution history + lifecycle events

The ``executions`` table is the source of truth (every attempt the
worker ran). Lifecycle events (state transitions, rejections) recorded
in ``mission.mission_metadata`` are merged in as supplementary entries
so a single endpoint answers "what happened to this mission, in order".
This is a read-only projection — it is deliberately NOT event-sourcing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from asf.db import get_db
from asf.errors import raise_http_error
from asf.models.enums import ExecutionStatus
from asf.models.execution import Execution
from asf.models.mission import Mission
from asf.schemas import LogEntry, MissionLogResponse

logger = logging.getLogger("asf.api.v1.logs")

router = APIRouter(prefix="/missions", tags=["logs"])

_ERROR_STATUSES = {ExecutionStatus.failed, ExecutionStatus.timeout}


def _iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


@router.get(
    "/{mission_id}/logs",
    response_model=MissionLogResponse,
    summary="Unified chronological log for a mission",
)
def get_mission_logs(
    mission_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> MissionLogResponse:
    """Return the mission's execution history + lifecycle events, in order.

    ``total`` is the full (unpaginated) entry count; ``entries`` is the
    requested page ordered oldest-first.
    """
    mission = db.get(Mission, mission_id)
    if mission is None:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "MISSION_NOT_FOUND",
            f"Mission {mission_id} not found",
        )

    entries: List[LogEntry] = []

    # 1. Executions — the source of truth.
    execs = (
        db.execute(
            select(Execution)
            .where(Execution.mission_id == mission_id)
            .order_by(Execution.created_at.asc())
        )
        .scalars()
        .all()
    )
    for e in execs:
        est = e.status if isinstance(e.status, ExecutionStatus) else ExecutionStatus(e.status)
        entries.append(LogEntry(
            timestamp=_iso(e.created_at),
            kind="execution",
            level="error" if est in _ERROR_STATUSES else "info",
            message=(
                f"task {e.task_id} attempt {e.attempt_number}: {est.value}"
            ),
            detail={
                "execution_id": str(e.id),
                "task_id": str(e.task_id),
                "agent_id": str(e.agent_id) if e.agent_id else None,
                "attempt_number": e.attempt_number,
                "status": est.value,
                "error": e.error,
            },
        ))

    # 2. Supplementary lifecycle events from mission metadata.
    # ``mission_metadata`` is a free-form, client-writable JSONB blob,
    # so ``transitions``/``rejections`` may not be the well-formed
    # list-of-dicts the writers produce. Coerce defensively — a
    # malformed blob must never turn this read-only endpoint into a
    # 500 (a persistent denial-of-read for that mission).
    md = mission.mission_metadata if isinstance(mission.mission_metadata, dict) else {}
    raw_transitions = md.get("transitions")
    for t in raw_transitions if isinstance(raw_transitions, list) else []:
        if not isinstance(t, dict):
            continue
        msg = f"{t.get('from')} → {t.get('to')}"
        if t.get("reason"):
            msg += f" ({t['reason']})"
        entries.append(LogEntry(
            timestamp=_iso(t.get("at")),
            kind="transition",
            level="info",
            message=msg,
            detail=t,
        ))
    raw_rejections = md.get("rejections")
    for r in raw_rejections if isinstance(raw_rejections, list) else []:
        if not isinstance(r, dict):
            continue
        entries.append(LogEntry(
            timestamp=_iso(r.get("at")),
            kind="rejection",
            level="warning",
            message=(
                f"rejected {r.get('from')} → {r.get('to')} "
                f"(rejection #{r.get('count')})"
            ),
            detail=r,
        ))

    # ISO-8601 strings sort chronologically; entries with no timestamp
    # (shouldn't happen, but be defensive) sort last.
    entries.sort(key=lambda x: x.timestamp or "9999")

    total = len(entries)
    page = entries[offset:offset + limit]
    return MissionLogResponse(mission_id=mission_id, total=total, entries=page)
