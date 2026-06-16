"""
soy.api.v1.executions
=====================

Execution log read endpoints.

The router is mounted under ``/api/v1/missions/{mission_id}`` by
:mod:`soy.api.v1.router`. It implements:

  * ``GET /api/v1/missions/{id}/executions``             — list
  * ``GET /api/v1/missions/{id}/executions/{eid}``       — read
  * ``GET /api/v1/missions/{id}/tasks/{tid}/executions`` — list for a task

The execution rows are inserted by the ASF worker when a task
is run; this router only reads them. The 3-try retry policy is
visible in the response: ``attempt_number`` increments 1 → 2
→ 3 across the executions rows for a single task.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from soy.db import get_db
from soy.errors import raise_http_error
from soy.models.enums import ExecutionStatus
from soy.models.execution import Execution
from soy.models.mission import Mission
from soy.models.task import Task
from soy.schemas import ExecutionList, ExecutionRead

logger = logging.getLogger("soy.api.v1.executions")

router = APIRouter(prefix="/missions", tags=["executions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_mission_or_404(db: Session, mission_id: uuid.UUID) -> Mission:
    mission = db.get(Mission, mission_id)
    if mission is None:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "MISSION_NOT_FOUND",
            f"Mission {mission_id} not found",
        )
    return mission


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/executions
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/executions",
    response_model=ExecutionList,
    summary="List execution rows for a mission",
)
def list_executions(
    mission_id: uuid.UUID,
    status_filter: Optional[ExecutionStatus] = Query(
        default=None, alias="status",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ExecutionList:
    """Return every execution row for the mission.

    ``limit`` defaults to 50 and is capped at 500 so a single
    dashboard request cannot page through millions of rows.
    """
    _get_mission_or_404(db, mission_id)
    base = select(Execution).where(Execution.mission_id == mission_id)
    count_stmt = (
        select(func.count()).select_from(Execution)
        .where(Execution.mission_id == mission_id)
    )
    if status_filter is not None:
        base = base.where(Execution.status == status_filter)
        count_stmt = count_stmt.where(Execution.status == status_filter)
    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            base.order_by(Execution.attempt_number.asc(), Execution.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return ExecutionList(
        total=total,
        items=[ExecutionRead.from_orm_execution(e) for e in rows],
    )


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/executions/{eid}
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/executions/{execution_id}",
    response_model=ExecutionRead,
    summary="Get a single execution row",
)
def get_execution(
    mission_id: uuid.UUID,
    execution_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> ExecutionRead:
    exec_row = db.get(Execution, execution_id)
    if exec_row is None or exec_row.mission_id != mission_id:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "EXECUTION_NOT_FOUND",
            f"Execution {execution_id} not found in mission {mission_id}",
        )
    return ExecutionRead.from_orm_execution(exec_row)


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/tasks/{tid}/executions
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/tasks/{task_id}/executions",
    response_model=ExecutionList,
    summary="List execution rows for a single task",
)
def list_task_executions(
    mission_id: uuid.UUID,
    task_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> ExecutionList:
    """Return execution rows for ``task_id``.

    The result is sorted by ``attempt_number`` so the caller
    can read the retry history in chronological order. ``limit``
    defaults to 50 (capped at 500); ``total`` is the unpaginated
    count.
    """
    _get_mission_or_404(db, mission_id)
    task = db.get(Task, task_id)
    if task is None or task.mission_id != mission_id:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "TASK_NOT_FOUND",
            f"Task {task_id} not found in mission {mission_id}",
        )
    base = select(Execution).where(Execution.task_id == task_id)
    count_stmt = (
        select(func.count()).select_from(Execution)
        .where(Execution.task_id == task_id)
    )
    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            base.order_by(Execution.attempt_number.asc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return ExecutionList(
        total=total,
        items=[ExecutionRead.from_orm_execution(e) for e in rows],
    )
