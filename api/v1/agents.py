"""
asf.api.v1.agents
=================

Agent CRUD and AgentTeam assembly endpoints.

The router is mounted under ``/api/v1/missions/{mission_id}/agents``
by :mod:`asf.api.v1.router`. It implements the surface described
in the validation contract for the agent orchestration engine:

  * ``POST /api/v1/missions/{id}/agents``                — create
  * ``GET  /api/v1/missions/{id}/agents``                — list
  * ``GET  /api/v1/missions/{id}/agents/{agent_id}``     — read one
  * ``POST /api/v1/missions/{id}/agents/team``           — assemble

The router uses :class:`asf.services.praisonai_worker.ASFWorker`
to construct PraisonAI agent instances; the worker is also where
the model resolution, sandbox tool list, and retry policy live.
The router itself only persists DB rows and serialises
responses.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from asf.db import get_db
from asf.errors import raise_http_error
from asf.models.agent import Agent
from asf.models.enums import AgentRole, AgentStatus
from asf.models.mission import Mission
from asf.schemas import (
    AgentCreate,
    AgentList,
    AgentRead,
    AgentTeamResponse,
)
from asf.services import mission_control_sync as mc_sync
from asf.services.praisonai_worker import (
    TEAM_ROLE_ORDER,
    get_worker,
)

logger = logging.getLogger("asf.api.v1.agents")

router = APIRouter(prefix="/missions", tags=["agents"])


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


def _get_agent_or_404(
    db: Session, mission_id: uuid.UUID, agent_id: uuid.UUID,
) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.mission_id != mission_id:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "AGENT_NOT_FOUND",
            f"Agent {agent_id} not found in mission {mission_id}",
        )
    return agent


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/agents
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/agents",
    response_model=AgentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agent for a mission",
)
def create_agent(
    mission_id: uuid.UUID,
    payload: AgentCreate,
    db: Session = Depends(get_db),
) -> AgentRead:
    """Create a new agent and persist it.

    The Pydantic schema restricts ``role`` to the four known
    enum members (``coder``, ``qa``, ``reviewer``,
    ``orchestrator``); anything else returns 422. The router
    does an additional check to surface a stable error code in
    case future enum members are added without updating the
    validation contract.

    After the row is inserted the ASF worker is asked to build
    a ``praisonaiagents.Agent`` so the contract that
    "constructing an agent builds a PraisonAI agent" is
    exercised at write time. The PraisonAI instance is *not*
    started here; the worker just returns the constructed
    object (the agent is later submitted to a workflow by
    :mod:`asf.api.v1.tasks`).
    """
    _get_mission_or_404(db, mission_id)

    # The Pydantic ``AgentRole`` enum already restricts the
    # values; this is a defence-in-depth check so the
    # structured error code is always ``INVALID_AGENT_ROLE``.
    allowed = {r.value for r in AgentRole}
    if payload.role.value not in allowed:
        raise_http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "INVALID_AGENT_ROLE",
            f"Agent role must be one of {sorted(allowed)}",
            allowed=sorted(allowed),
        )

    agent = Agent(
        mission_id=mission_id,
        name=payload.name,
        role=payload.role,
        model=payload.model,
        llm_config=payload.llm_config,
        tool_config=payload.tool_config,
        system_prompt=payload.system_prompt,
        sandbox=bool(payload.sandbox),
        status=AgentStatus.idle,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    # Build the PraisonAI agent so the construction path is
    # exercised. The instance is discarded; the worker's
    # executor pool keeps the underlying resources alive so
    # re-using the agent later is cheap.
    try:
        get_worker().build_praisonai_agent(agent.id)
    except Exception as exc:  # noqa: BLE001 — construction failures are non-fatal
        logger.warning(
            "PraisonAI agent construction failed for agent %s: %s",
            agent.id, exc,
        )
        # The DB row is preserved; the construction failure
        # is recorded in the agent's tool_config blob so the
        # dashboard can surface it.
        cfg = dict(agent.tool_config or {})
        cfg["praisonai_init_error"] = str(exc)
        agent.tool_config = cfg
        db.commit()
        db.refresh(agent)

    # Best-effort Mission Control sync (gated; no-op when disabled).
    mc_sync.sync_agent(agent)
    return AgentRead.from_orm_agent(agent)


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/agents
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/agents",
    response_model=AgentList,
    summary="List agents in a mission",
)
def list_agents(
    mission_id: uuid.UUID,
    role: Optional[AgentRole] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> AgentList:
    """Return agents for ``mission_id``.

    Agents from other missions are excluded by the
    ``WHERE mission_id = :mission_id`` clause; the unit tests
    cover the cross-mission leak case. ``limit`` defaults to 50
    (capped at 500) so a mission with many agents cannot return an
    unbounded result set; ``total`` is the unpaginated count.
    """
    _get_mission_or_404(db, mission_id)
    base = select(Agent).where(Agent.mission_id == mission_id)
    count_stmt = (
        select(func.count()).select_from(Agent)
        .where(Agent.mission_id == mission_id)
    )
    if role is not None:
        base = base.where(Agent.role == role)
        count_stmt = count_stmt.where(Agent.role == role)
    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            base.order_by(Agent.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return AgentList(
        total=total,
        items=[AgentRead.from_orm_agent(a) for a in rows],
    )


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}/agents/{agent_id}
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}/agents/{agent_id}",
    response_model=AgentRead,
    summary="Get a single agent",
)
def get_agent(
    mission_id: uuid.UUID,
    agent_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> AgentRead:
    agent = _get_agent_or_404(db, mission_id, agent_id)
    return AgentRead.from_orm_agent(agent)


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/agents/team
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/agents/team",
    response_model=AgentTeamResponse,
    summary="Assemble the AgentTeam for a mission",
)
def assemble_team(
    mission_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> AgentTeamResponse:
    """Assemble the AgentTeam for ``mission_id``.

    The endpoint persists no rows. It calls the worker's
    :meth:`assemble_team` to build the canonical team in the
    order ``orchestrator → coder → qa → reviewer`` (missing
    roles are omitted) and returns the assembly.

    The endpoint also reports ``parallel_supported = True``
    when the team has at least two agents so the dashboard
    can render the "run in parallel" toggle.
    """
    _get_mission_or_404(db, mission_id)
    worker = get_worker()
    team_ids, order = worker.assemble_team(mission_id, db=db)
    if not team_ids:
        return AgentTeamResponse(
            mission_id=mission_id,
            team=[],
            order=[],
            parallel_supported=False,
            message="No agents registered for this mission",
        )
    rows = (
        db.execute(
            select(Agent).where(Agent.id.in_(team_ids))
        )
        .scalars()
        .all()
    )
    by_id = {a.id: a for a in rows}
    ordered = [by_id[i] for i in team_ids if i in by_id]
    return AgentTeamResponse(
        mission_id=mission_id,
        team=[AgentRead.from_orm_agent(a) for a in ordered],
        order=order,
        parallel_supported=len(ordered) > 1,
        message=(
            f"Assembled team in canonical order: {' → '.join(order)}"
        ),
    )
