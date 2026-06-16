"""
soy.api.v1.tasks
================

Task CRUD and execution endpoints.

The router is mounted under ``/api/v1/missions/{mission_id}`` by
:mod:`soy.api.v1.router`. It implements:

  * ``POST /api/v1/missions/{id}/agents/{aid}/tasks``  — create
  * ``GET  /api/v1/missions/{id}/tasks``                — list
  * ``GET  /api/v1/missions/{id}/tasks/{tid}``          — read
  * ``POST /api/v1/missions/{id}/tasks/{tid}/execute``  — execute
  * ``POST /api/v1/missions/{id}/tasks/execute-all``    — parallel

Each task row maps to a ``praisonaiagents.Task`` at execution
time. The 3-try retry rule, timeouts, sandbox tool list, and
escalation policy all live in
:mod:`soy.services.praisonai_worker`; the router delegates to
that worker and serialises the result.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from soy.db import get_db
from soy.errors import raise_http_error
from soy.models.agent import Agent
from soy.models.enums import TaskStatus
from soy.models.mission import Mission
from soy.models.task import Task
from soy.schemas import (
    TaskCreate,
    TaskExecuteRequest,
    TaskExecuteResponse,
    TaskList,
    TaskRead,
)
from soy.services import mission_control_sync as mc_sync
from soy.services.praisonai_worker import (
    TaskExecutionResult,
    get_worker,
)

logger = logging.getLogger("soy.api.v1.tasks")

router = APIRouter(prefix="/missions", tags=["tasks"])


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


def _get_task_or_404(
    db: Session, mission_id: uuid.UUID, task_id: uuid.UUID,
) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.mission_id != mission_id:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "TASK_NOT_FOUND",
            f"Task {task_id} not found in mission {mission_id}",
        )
    return task


def _result_to_response(result: TaskExecutionResult) -> TaskExecuteResponse:
    return TaskExecuteResponse(
        task_id=result.task_id,
        status=result.status,
        execution_id=result.execution_id,
        attempt_number=result.attempt_number,
        output=result.output,
        error=result.error,
        retry_scheduled=result.retry_scheduled,
        escalated=result.escalated,
        attempt_count=result.attempt_count,
        message=result.message,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/agents/{aid}/tasks
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/agents/{agent_id}/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task for an agent",
)
def create_task(
    mission_id: uuid.UUID,
    agent_id: uuid.UUID,
    payload: TaskCreate,
    db: Session = Depends(get_db),
) -> TaskRead:
    """Create a new task assigned to ``agent_id``.

    The agent must belong to the mission; a mismatch returns
    404 ``AGENT_NOT_FOUND``. The ``depends_on`` list is
    normalised to a JSON-serialisable list of UUID strings so
    it round-trips cleanly through the SQLite/PostgreSQL
    JSONB column.
    """
    _get_mission_or_404(db, mission_id)
    agent = db.get(Agent, agent_id)
    if agent is None or agent.mission_id != mission_id:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "AGENT_NOT_FOUND",
            f"Agent {agent_id} not found in mission {mission_id}",
        )

    deps: Optional[list] = None
    if payload.depends_on is not None:
        dep_uuids = [uuid.UUID(str(d)) for d in payload.depends_on]
        if dep_uuids:
            # Every dependency must be an existing task in THIS mission.
            # A foreign/cross-mission/nonexistent id would corrupt the
            # execute-all dependency graph (it can never be satisfied,
            # so the dependent would only ever run via the cycle-break
            # fallback) and bypass the ownership checks enforced
            # elsewhere.
            found = set(
                db.execute(
                    select(Task.id).where(
                        Task.mission_id == mission_id,
                        Task.id.in_(dep_uuids),
                    )
                ).scalars().all()
            )
            missing = [str(d) for d in dep_uuids if d not in found]
            if missing:
                raise_http_error(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "INVALID_DEPENDENCY",
                    "depends_on references task(s) that do not exist in "
                    f"this mission: {missing}",
                    missing=missing,
                )
        deps = [str(d) for d in dep_uuids]

    task = Task(
        mission_id=mission_id,
        agent_id=agent_id,
        description=payload.description,
        expected_output=payload.expected_output,
        depends_on=deps,
        config=payload.config,
        status=TaskStatus.pending,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    # Best-effort Mission Control sync (gated; no-op when disabled).
    mc_sync.sync_task(task)
    return TaskRead.from_orm_task(task)


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/tasks
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/tasks",
    response_model=TaskList,
    summary="List tasks in a mission",
)
def list_tasks(
    mission_id: uuid.UUID,
    status_filter: Optional[TaskStatus] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> TaskList:
    """Return tasks for ``mission_id`` (cross-agent).

    ``limit`` defaults to 50 (capped at 500) so a mission with many
    tasks cannot return an unbounded result set; ``total`` is the
    unpaginated count.
    """
    _get_mission_or_404(db, mission_id)
    base = select(Task).where(Task.mission_id == mission_id)
    count_stmt = (
        select(func.count()).select_from(Task)
        .where(Task.mission_id == mission_id)
    )
    if status_filter is not None:
        base = base.where(Task.status == status_filter)
        count_stmt = count_stmt.where(Task.status == status_filter)
    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            base.order_by(Task.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return TaskList(
        total=total,
        items=[TaskRead.from_orm_task(t) for t in rows],
    )


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/tasks/{tid}
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/tasks/{task_id}",
    response_model=TaskRead,
    summary="Get a single task",
)
def get_task(
    mission_id: uuid.UUID,
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> TaskRead:
    task = _get_task_or_404(db, mission_id, task_id)
    return TaskRead.from_orm_task(task)


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/tasks/{tid}/execute
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/tasks/{task_id}/execute",
    response_model=TaskExecuteResponse,
    summary="Execute a single task via PraisonAI",
)
def execute_task(
    mission_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskExecuteRequest,
    db: Session = Depends(get_db),
) -> TaskExecuteResponse:
    """Execute a single task.

    The 3-try retry rule, the timeout, and the escalation
    policy are all enforced by the worker. The router simply
    delegates and serialises the result.

    The endpoint does **not** run a separate parallel workflow
    for a single task — the ``parallel`` flag is accepted so
    the same client code path works for the
    ``/tasks/execute-all`` endpoint, but the underlying
    ``Agents`` workflow is always built with one task, so
    ``process="parallel"`` has no observable effect here.
    """
    _get_mission_or_404(db, mission_id)
    _get_task_or_404(db, mission_id, task_id)
    worker = get_worker()
    result = worker.execute_task(
        task_id,
        timeout_seconds=payload.timeout_seconds,
        parallel=bool(payload.parallel),
    )
    # Best-effort Mission Control sync of the post-execution task state.
    mc_sync.sync_task(db.get(Task, task_id))
    return _result_to_response(result)


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/tasks/execute-all
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/tasks/execute-all",
    response_model=List[TaskExecuteResponse],
    summary="Execute every task in a mission",
)
def execute_all_tasks(
    mission_id: uuid.UUID,
    payload: TaskExecuteRequest,
    db: Session = Depends(get_db),
) -> List[TaskExecuteResponse]:
    """Execute every task in ``mission_id`` in dependency order.

    When ``payload.parallel = True`` (the default) independent
    tasks (no shared ``depends_on`` edges) run concurrently in
    a thread-pool executor. The endpoint waits for every task
    to reach a terminal status before returning.
    """
    _get_mission_or_404(db, mission_id)
    worker = get_worker()
    results = worker.execute_mission_tasks(
        mission_id,
        parallel=bool(payload.parallel),
        timeout_seconds=payload.timeout_seconds,
    )
    return [_result_to_response(r) for r in results]
