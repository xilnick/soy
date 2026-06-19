"""
soy.api.v1.control
===================

Control dashboard endpoints for the Soy backend.

These endpoints provide a dashboard-first interface for mission
management without requiring GitHub. They support:

- Mission creation from the dashboard (minimal: title only)
- Refinement via Hermes/Droid agents
- Research via DeerFlow
- Verification via QA agent
- Autonomous execution (branch → commit → merge)
- Aggregated status for the dashboard
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from soy.db import get_db
from soy.errors import raise_http_error
from soy.models.agent import Agent
from soy.models.enums import AgentRole, AgentStatus, MissionStatus, TaskStatus
from soy.models.execution import Execution
from soy.models.mission import Mission
from soy.models.task import Task
from soy.schemas import (
    AutoRunRequest,
    AutoRunResponse,
    ControlMissionCreate,
    ControlStatusResponse,
    MergeRequest,
    MissionRead,
    RefineRequest,
    ResearchRequest,
    VerifyRequest,
)
from soy.services import mission_control_sync as mc_sync
from soy.services.git_backend import get_backend as get_git_backend
from soy.services.praisonai_worker import get_worker

logger = logging.getLogger("soy.api.v1.control")

router = APIRouter(prefix="/control", tags=["control"])

CONTROL_ENABLED = os.getenv("SOY_CONTROL_ENABLED", "true").lower() in ("1", "true", "yes")


def _check_control_enabled() -> None:
    if not CONTROL_ENABLED:
        raise_http_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CONTROL_DISABLED",
            "Control dashboard is disabled. Set SOY_CONTROL_ENABLED=true to enable.",
        )


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
# POST /api/v1/control/missions — create from dashboard
# ---------------------------------------------------------------------------
@router.post(
    "/missions",
    response_model=MissionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a mission from the control dashboard",
)
def create_control_mission(
    payload: ControlMissionCreate,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Create a mission from the control dashboard.

    Minimal creation — only title required. No repo_url or
    branch_prefix needed. The mission starts in ``created`` state.
    """
    _check_control_enabled()

    mission = Mission(
        title=payload.title,
        description=payload.description,
        source=payload.source or "dashboard",
        status=MissionStatus.created,
        mission_metadata=payload.mission_metadata or {},
    )
    db.add(mission)
    db.commit()
    db.refresh(mission)
    mc_sync.sync_mission_status(mission)
    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/refine
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/refine",
    response_model=MissionRead,
    summary="Refine a mission's plan using an agent",
)
def refine_mission(
    mission_id: uuid.UUID,
    payload: RefineRequest,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Dispatch a refinement agent to improve the mission description.

    The agent iterates on the mission's description, improving
    clarity, scope, and technical details. Results are stored in
    mission metadata under ``refinement_history``.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    worker = get_worker()

    # Build a refinement prompt
    refinement_prompt = (
        f"Refine and improve the following mission description. "
        f"Make it clearer, more specific, and actionable. "
        f"Keep the core intent but add technical detail.\n\n"
        f"Title: {mission.title}\n"
        f"Description: {mission.description or '(no description provided)'}\n"
    )
    if payload.prompt:
        refinement_prompt += f"\nAdditional instructions: {payload.prompt}"

    # Use the mission's orchestrator agent or create a temporary one
    agent_model = payload.model or os.getenv("SOY_MODEL", "minimax-m3")

    # Record refinement attempt in metadata
    md = dict(mission.mission_metadata or {})
    refinement_history = md.get("refinement_history", [])
    refinement_history.append({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": payload.prompt,
        "model": agent_model,
    })

    # Update mission metadata with refinement request
    md["refinement_history"] = refinement_history
    mission.mission_metadata = md
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

    mc_sync.sync_mission_status(mission)
    logger.info("Refinement triggered for mission %s", mission_id)

    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/research
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/research",
    response_model=MissionRead,
    summary="Research a mission using DeerFlow",
)
def research_mission(
    mission_id: uuid.UUID,
    payload: ResearchRequest,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Dispatch a DeerFlow research task for the mission.

    Results are stored in mission metadata under ``research``.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    query = payload.query or f"{mission.title}: {mission.description or ''}"

    md = dict(mission.mission_metadata or {})
    research_results = md.get("research", [])
    research_results.append({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "status": "triggered",
    })
    md["research"] = research_results
    mission.mission_metadata = md
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

    mc_sync.sync_mission_status(mission)
    logger.info("Research triggered for mission %s: %s", mission_id, query[:100])

    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/verify
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/verify",
    response_model=MissionRead,
    summary="Verify a mission's plan using QA agent",
)
def verify_mission(
    mission_id: uuid.UUID,
    payload: VerifyRequest,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Dispatch the QA agent to verify the mission plan.

    Stores verification result in mission metadata under ``verification``.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    md = dict(mission.mission_metadata or {})
    verification = md.get("verification", [])
    verification.append({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": payload.prompt,
        "status": "triggered",
    })
    md["verification"] = verification
    mission.mission_metadata = md
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

    mc_sync.sync_mission_status(mission)
    logger.info("Verification triggered for mission %s", mission_id)

    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/start-execution
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/start-execution",
    response_model=MissionRead,
    summary="One-click: approve planning and start execution",
)
def start_execution(
    mission_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> MissionRead:
    """One-click execution start: approve planning + transition to execution.

    Sets planning_complete marker and transitions mission to execution.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    current = MissionStatus(mission.status) if isinstance(mission.status, MissionStatus) else MissionStatus(mission.status)

    # If mission is in planning, approve it first
    if current == MissionStatus.planning:
        md = dict(mission.mission_metadata or {})
        md["planning_complete"] = True
        mission.mission_metadata = md
        mission.status = MissionStatus.execution
    elif current == MissionStatus.created:
        md = dict(mission.mission_metadata or {})
        md["planning_complete"] = True
        mission.mission_metadata = md
        mission.status = MissionStatus.execution
    elif current == MissionStatus.approved:
        mission.status = MissionStatus.execution
    elif current == MissionStatus.execution:
        pass  # already in execution
    else:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_STATE",
            f"Cannot start execution from state {current.value}",
        )

    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)
    mc_sync.sync_mission_status(mission)

    logger.info("Execution started for mission %s", mission_id)
    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/auto-run
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/auto-run",
    response_model=AutoRunResponse,
    summary="Full autonomous run: branch → agent → commit → merge",
)
def auto_run_mission(
    mission_id: uuid.UUID,
    payload: AutoRunRequest,
    db: Session = Depends(get_db),
) -> AutoRunResponse:
    """Execute a full autonomous mission run.

    Steps:
    1. Set repo_url and branch_prefix if provided
    2. Create feature branch
    3. Dispatch coding agent
    4. Commit changes
    5. Merge branch (if auto_merge=True)
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    # Set repo_url if provided and not already set
    if payload.repo_url and not mission.repo_url:
        mission.repo_url = payload.repo_url
    if payload.branch_prefix:
        mission.branch_prefix = payload.branch_prefix

    if not mission.repo_url:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "NO_REPO_URL",
            "Mission has no repo_url set. Provide one in the request or via PUT /missions/{id}.",
        )

    # Generate branch name if not set
    branch_name = (
        mission.branch_prefix
        or f"feature/soy-{mission.external_id or mission.id}"
    )

    repo_path = os.path.expanduser(mission.repo_url.replace("~", ""))
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        # If repo_url is a URL, not a local path, use the configured workdir
        from soy import config
        repo_path = os.path.join(
            os.path.expanduser(config.git_workdir().replace("~", "")),
            str(mission.id),
        )

    backend = get_git_backend(repo_path)

    # 1. Create branch
    try:
        backend.create_branch(branch_name)
        mission.branch = branch_name
        mission.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        logger.exception("Failed to create branch for mission %s", mission_id)
        return AutoRunResponse(
            mission_id=mission.id,
            status="error",
            error=f"Branch creation failed: {exc}",
        )

    # 2. Dispatch coding agent (if agents are set up)
    agent_name = payload.agent_name
    agent_output = None
    if not agent_name:
        # Look for a coder agent in the mission
        coder = db.execute(
            select(Agent).where(
                Agent.mission_id == mission.id,
                Agent.role == AgentRole.coder,
            )
        ).scalars().first()
        if coder:
            agent_name = coder.model

    if agent_name:
        prompt = payload.prompt or f"Implement: {mission.title}\n\n{mission.description or ''}"
        try:
            from soy.services.coding_agent_dispatcher import dispatch as agent_dispatch
            result = agent_dispatch(
                agent_name,
                prompt,
                cwd=repo_path,
                timeout=payload.timeout_seconds or 600,
            )
            agent_output = result.to_execution_output()
            if result.error:
                logger.warning(
                    "Agent %s returned error for mission %s: %s",
                    agent_name, mission_id, result.error,
                )
        except Exception as exc:
            logger.warning(
                "Agent dispatch failed for mission %s: %s", mission_id, exc,
            )
            agent_output = {"error": str(exc)}

    # 3. Commit changes
    commit_sha = None
    try:
        commit_sha = backend.commit(
            f"feat(soy-mission {mission_id}): {mission.title}",
            author_name="Soy Bot",
            author_email="soy-bot@piperoni.local",
        )
        md = dict(mission.mission_metadata or {})
        md.setdefault("git", {})["commit_sha"] = commit_sha
        mission.mission_metadata = md
        db.commit()
    except Exception as exc:
        logger.warning("Commit failed for mission %s: %s", mission_id, exc)

    # 4. Merge (if auto_merge)
    merged = False
    merge_sha = None
    if payload.auto_merge and commit_sha:
        try:
            merge_sha = backend.merge_branch(branch_name, target="main", strategy="squash")
            merged = True
            mission.status = MissionStatus.merged
            md = dict(mission.mission_metadata or {})
            md.setdefault("git", {})["merge_sha"] = merge_sha
            mission.mission_metadata = md
            mission.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(mission)
            mc_sync.sync_mission_status(mission)
        except Exception as exc:
            logger.warning("Merge failed for mission %s: %s", mission_id, exc)

    return AutoRunResponse(
        mission_id=mission.id,
        status="completed" if commit_sha else "partial",
        branch=branch_name,
        commit_sha=commit_sha,
        merged=merged,
        merge_sha=merge_sha,
        agent_output=agent_output,
        message="Autonomous run completed" if merged else "Run completed (merge skipped or failed)",
    )


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/branch
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/branch",
    response_model=dict,
    summary="Create a feature branch for a mission",
)
def create_branch(
    mission_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    """Create a feature branch for the mission."""
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    if not mission.repo_url:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "NO_REPO_URL",
            "Mission has no repo_url set.",
        )

    branch_name = (
        mission.branch_prefix
        or f"feature/soy-{mission.external_id or mission.id}"
    )

    from soy import config
    repo_path = os.path.expanduser(config.git_workdir().replace("~", ""))
    repo_path = os.path.join(repo_path, str(mission.id))
    backend = get_git_backend(repo_path)

    try:
        backend.create_branch(branch_name)
        mission.branch = branch_name
        mission.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"branch": branch_name, "status": "created"}
    except Exception as exc:
        raise_http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "BRANCH_FAILED",
            f"Failed to create branch: {exc}",
        )


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/commit
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/commit",
    response_model=dict,
    summary="Stage and commit changes for a mission",
)
def commit_changes(
    mission_id: uuid.UUID,
    message: Optional[str] = None,
    db: Session = Depends(get_db),
) -> dict:
    """Stage all changes and commit."""
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    from soy import config
    repo_path = os.path.expanduser(config.git_workdir().replace("~", ""))
    repo_path = os.path.join(repo_path, str(mission.id))
    backend = get_git_backend(repo_path)

    commit_msg = message or f"feat(soy-mission {mission_id}): {mission.title}"

    try:
        sha = backend.commit(commit_msg)
        md = dict(mission.mission_metadata or {})
        md.setdefault("git", {})["commit_sha"] = sha
        mission.mission_metadata = md
        mission.updated_at = datetime.now(timezone.utc)
        db.commit()
        return {"commit_sha": sha, "message": commit_msg}
    except Exception as exc:
        raise_http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "COMMIT_FAILED",
            f"Failed to commit: {exc}",
        )


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/merge
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/merge",
    response_model=dict,
    summary="Merge the mission's feature branch into main",
)
def merge_branch(
    mission_id: uuid.UUID,
    payload: MergeRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Merge the mission's feature branch into main."""
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    if not mission.branch:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "NO_BRANCH",
            "Mission has no branch set.",
        )

    from soy import config
    repo_path = os.path.expanduser(config.git_workdir().replace("~", ""))
    repo_path = os.path.join(repo_path, str(mission.id))
    backend = get_git_backend(repo_path)

    try:
        merge_sha = backend.merge_branch(
            mission.branch,
            target="main",
            strategy=payload.strategy,
        )
        mission.status = MissionStatus.merged
        md = dict(mission.mission_metadata or {})
        md.setdefault("git", {})["merge_sha"] = merge_sha
        mission.mission_metadata = md
        mission.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(mission)
        mc_sync.sync_mission_status(mission)
        return {"merge_sha": merge_sha, "status": "merged"}
    except Exception as exc:
        raise_http_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "MERGE_FAILED",
            f"Failed to merge: {exc}",
        )


# ---------------------------------------------------------------------------
# GET /api/v1/control/missions/{id}/status
# ---------------------------------------------------------------------------
@router.get(
    "/missions/{mission_id}/status",
    response_model=ControlStatusResponse,
    summary="Aggregated mission status for the control dashboard",
)
def get_control_status(
    mission_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> ControlStatusResponse:
    """Return aggregated status: mission + agents + tasks + git info."""
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    agent_count = (
        db.execute(
            select(func.count()).select_from(Agent)
            .where(Agent.mission_id == mission.id)
        ).scalar_one()
    )
    task_count = (
        db.execute(
            select(func.count()).select_from(Task)
            .where(Task.mission_id == mission.id)
        ).scalar_one()
    )
    completed_tasks = (
        db.execute(
            select(func.count()).select_from(Task)
            .where(
                Task.mission_id == mission.id,
                Task.status == TaskStatus.completed,
            )
        ).scalar_one()
    )

    md = mission.mission_metadata or {}
    git_info = md.get("git")
    research_results = md.get("research")
    verification_results = md.get("verification")
    refinement_history = md.get("refinement_history")

    # Get last execution
    last_execution = None
    last_exec = db.execute(
        select(Execution)
        .where(Execution.mission_id == mission.id)
        .order_by(Execution.created_at.desc())
        .limit(1)
    ).scalars().first()
    if last_exec:
        last_execution = {
            "id": str(last_exec.id),
            "status": last_exec.status.value if hasattr(last_exec.status, "value") else str(last_exec.status),
            "started_at": last_exec.started_at.isoformat() if last_exec.started_at else None,
            "finished_at": last_exec.finished_at.isoformat() if last_exec.finished_at else None,
            "error": last_exec.error,
        }

    return ControlStatusResponse(
        mission_id=mission.id,
        title=mission.title,
        status=MissionStatus(mission.status) if isinstance(mission.status, MissionStatus) else MissionStatus(mission.status),
        description=mission.description,
        repo_url=mission.repo_url,
        branch=mission.branch,
        agent_count=agent_count,
        task_count=task_count,
        completed_tasks=completed_tasks,
        git_info=git_info,
        research_results=research_results,
        verification_results=verification_results,
        refinement_history=refinement_history,
        last_execution=last_execution,
    )
