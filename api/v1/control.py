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
from sqlalchemy.orm.attributes import flag_modified

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
    ReviewPlanRequest,
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

    Uses the coding agent dispatcher to invoke the configured coding
    agent with a refinement prompt. The refined output is stored in
    mission metadata under ``refinement_history``.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    agent_model = payload.model or os.getenv("SOY_MODEL", "minimax-m3")

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

    # Record refinement attempt in metadata
    md = dict(mission.mission_metadata or {})
    refinement_history = md.get("refinement_history", [])
    entry = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": payload.prompt,
        "model": agent_model,
    }

    # Dispatch via coding agent dispatcher (best-effort)
    try:
        from soy.services.coding_agent_dispatcher import (
            AgentNotFoundError,
            dispatch as agent_dispatch,
        )
        # Try dispatching as a coding agent first
        result = agent_dispatch(
            agent_model,
            refinement_prompt,
            timeout=payload.timeout_seconds or 300,
        )
        entry["status"] = "completed" if not result.error else "error"
        entry["output"] = result.stdout[:2000] if result.stdout else ""
        entry["error"] = result.error
        entry["duration_seconds"] = result.duration_seconds
        entry["exit_code"] = result.exit_code

        # Update description if refinement succeeded
        if not result.error and result.stdout.strip():
            # Extract the refined description from agent output
            new_desc = result.stdout.strip()
            if new_desc:
                mission.description = new_desc
    except (AgentNotFoundError, FileNotFoundError):
        # No coding agent manifest found — record as triggered
        # (planning agent will pick it up later)
        entry["status"] = "triggered"
        entry["note"] = "no coding agent manifest found; will use planning phase agent"
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        logger.warning("Refinement dispatch failed for mission %s: %s", mission_id, exc)

    refinement_history.append(entry)
    md["refinement_history"] = refinement_history
    mission.mission_metadata = md
    flag_modified(mission, "mission_metadata")
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

    mc_sync.sync_mission_status(mission)
    logger.info("Refinement completed for mission %s", mission_id)

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
    """Dispatch a research task for the mission.

    Dispatches to the configured research agent (default: hermes) via
    the coding agent dispatcher, and also calls the DeerFlow sandbox
    API (gated by SOY_DEERFLOW_ENABLED) for deeper research.
    Results are stored in mission metadata under ``research``.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    query = payload.query or f"{mission.title}: {mission.description or ''}"

    md = dict(mission.mission_metadata or {})
    research_results = md.get("research", [])
    entry = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
    }

    from soy import config as soy_config

    # 1. Dispatch via research agent (hermes by default) for quick research
    research_agent = payload.agent or soy_config.research_agent()
    try:
        from soy.services.coding_agent_dispatcher import dispatch as agent_dispatch
        research_prompt = (
            f"Research the following topic and provide a concise summary "
            f"with key findings, relevant links, and actionable insights.\n\n"
            f"{query}"
        )
        result = agent_dispatch(
            research_agent,
            research_prompt,
            model=payload.model,
            timeout=payload.timeout_seconds or 300,
        )
        entry["agent"] = research_agent
        entry["status"] = "completed" if not result.error else "error"
        entry["output"] = result.stdout[:4000] if result.stdout else ""
        entry["error"] = result.error
        entry["duration_seconds"] = result.duration_seconds
        entry["exit_code"] = result.exit_code
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        logger.warning("Research agent dispatch failed for mission %s: %s", mission_id, exc)

    # 2. Also dispatch via DeerFlow client for deeper sandbox research (best-effort, gated)
    try:
        from soy.services.deerflow_client import DeerFlowClient
        client = DeerFlowClient()
        df_result = client.trigger_sandbox_task(
            task_id=str(mission.id),
            description=query,
            metadata={"mission_id": str(mission.id), "source": "control_research"},
        )
        if df_result is not None:
            entry["deerflow_status"] = "completed"
            entry["deerflow_result"] = df_result
        else:
            entry["deerflow_status"] = "triggered"
            entry["deerflow_note"] = "DeerFlow not available or returned no result"
    except Exception as exc:
        entry["deerflow_status"] = "error"
        entry["deerflow_error"] = str(exc)
        logger.warning("DeerFlow research failed for mission %s: %s", mission_id, exc)

    research_results.append(entry)
    md["research"] = research_results
    mission.mission_metadata = md
    flag_modified(mission, "mission_metadata")
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
    """Dispatch the QA/reviewer agent to verify the mission plan.

    Uses the coding agent dispatcher with a verification prompt.
    Stores verification result in mission metadata under ``verification``.
    """
    _check_control_enabled()
    mission = _get_mission_or_404(db, mission_id)

    agent_model = payload.model or os.getenv("SOY_MODEL", "minimax-m3")

    verify_prompt = payload.prompt or (
        f"Review the following mission plan for completeness and feasibility.\n\n"
        f"Title: {mission.title}\n"
        f"Description: {mission.description or '(no description)'}\n\n"
        f"Check for:\n"
        f"1. Clear problem statement\n"
        f"2. Specific, measurable acceptance criteria\n"
        f"3. Technical feasibility\n"
        f"4. Missing dependencies or assumptions\n"
        f"Respond with PASS or FAIL and a brief explanation."
    )

    md = dict(mission.mission_metadata or {})
    verification = md.get("verification", [])
    entry = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": payload.prompt,
        "model": agent_model,
    }

    # Dispatch via coding agent dispatcher (best-effort)
    try:
        from soy.services.coding_agent_dispatcher import (
            AgentNotFoundError,
            dispatch as agent_dispatch,
        )
        result = agent_dispatch(
            agent_model,
            verify_prompt,
            timeout=payload.timeout_seconds or 300,
        )
        entry["status"] = "completed" if not result.error else "error"
        entry["output"] = result.stdout[:2000] if result.stdout else ""
        entry["error"] = result.error
        entry["exit_code"] = result.exit_code
        entry["duration_seconds"] = result.duration_seconds

        # Determine pass/fail from output
        if not result.error and result.stdout:
            if "PASS" in result.stdout.upper()[:50]:
                entry["verdict"] = "pass"
            elif "FAIL" in result.stdout.upper()[:50]:
                entry["verdict"] = "fail"
            else:
                entry["verdict"] = "unclear"

        # Set audit_passed flag if verification passed
        if entry.get("verdict") == "pass":
            md["audit_passed"] = True
    except (AgentNotFoundError, FileNotFoundError):
        entry["status"] = "triggered"
        entry["note"] = "no coding agent manifest found; will use planning phase agent"
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        logger.warning("Verification dispatch failed for mission %s: %s", mission_id, exc)

    verification.append(entry)
    md["verification"] = verification
    mission.mission_metadata = md
    flag_modified(mission, "mission_metadata")
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

    mc_sync.sync_mission_status(mission)
    logger.info("Verification triggered for mission %s", mission_id)

    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# POST /api/v1/control/missions/{id}/review-plan
# ---------------------------------------------------------------------------
@router.post(
    "/missions/{mission_id}/review-plan",
    response_model=MissionRead,
    summary="Review the mission plan using a dedicated review model",
)
def review_plan(
    mission_id: uuid.UUID,
    payload: ReviewPlanRequest,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Review the mission plan before execution using a dedicated model.

    Gated by ``SOY_REVIEW_MODEL``. When the env var is empty, the
    endpoint returns 200 with ``review_disabled`` status. When set
    (e.g. ``z-ai/glm-5.2``), dispatches the coding agent dispatcher
    with the review model and a plan-review prompt.

    Stores result in ``mission_metadata['review']`` and sets
    ``mission_metadata['review_passed'] = True`` when the verdict is PASS.
    """
    _check_control_enabled()

    from soy import config as soy_config

    if not soy_config.review_enabled() and not payload.model:
        # Review not configured — return success with a skip indicator
        mission = _get_mission_or_404(db, mission_id)
        md = dict(mission.mission_metadata or {})
        md.setdefault("review", []).append({
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "skipped",
            "reason": "SOY_REVIEW_MODEL not configured",
        })
        mission.mission_metadata = md
        flag_modified(mission, "mission_metadata")
        db.commit()
        db.refresh(mission)
        return MissionRead.from_orm_mission(mission)

    mission = _get_mission_or_404(db, mission_id)
    review_model = payload.model or soy_config.review_model()

    review_prompt = payload.prompt or (
        f"Review the following mission plan for completeness, feasibility, "
        f"and technical soundness before implementation.\n\n"
        f"Title: {mission.title}\n"
        f"Description: {mission.description or '(no description)'}\n\n"
        f"Check for:\n"
        f"1. Clear problem statement and well-defined scope\n"
        f"2. Specific, measurable acceptance criteria\n"
        f"3. Technical feasibility and correct approach\n"
        f"4. Missing dependencies, assumptions, or edge cases\n"
        f"5. Security and performance considerations\n\n"
        f"Respond with PASS or FAIL as the first word, followed by a brief explanation."
    )

    md = dict(mission.mission_metadata or {})
    review_history = md.get("review", [])
    entry = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": review_model,
        "prompt": payload.prompt,
    }

    try:
        from soy.services.coding_agent_dispatcher import (
            AgentNotFoundError,
            dispatch as agent_dispatch,
        )
        result = agent_dispatch(
            review_model,
            review_prompt,
            timeout=payload.timeout_seconds or 300,
        )
        entry["status"] = "completed" if not result.error else "error"
        entry["output"] = result.stdout[:4000] if result.stdout else ""
        entry["error"] = result.error
        entry["exit_code"] = result.exit_code
        entry["duration_seconds"] = result.duration_seconds

        # Determine pass/fail from output
        if not result.error and result.stdout:
            upper = result.stdout.strip().upper()
            if upper.startswith("PASS"):
                entry["verdict"] = "pass"
            elif upper.startswith("FAIL"):
                entry["verdict"] = "fail"
            else:
                entry["verdict"] = "unclear"

        # Set review_passed flag if review passed
        if entry.get("verdict") == "pass":
            md["review_passed"] = True
    except (AgentNotFoundError, FileNotFoundError):
        entry["status"] = "error"
        entry["error"] = f"No agent manifest for review model '{review_model}'"
        logger.warning("Review plan agent not found for mission %s: %s", mission_id, review_model)
    except Exception as exc:
        entry["status"] = "error"
        entry["error"] = str(exc)
        logger.warning("Review plan dispatch failed for mission %s: %s", mission_id, exc)

    review_history.append(entry)
    md["review"] = review_history
    mission.mission_metadata = md
    flag_modified(mission, "mission_metadata")
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

    mc_sync.sync_mission_status(mission)
    logger.info("Plan review triggered for mission %s (model=%s)", mission_id, review_model)

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
        flag_modified(mission, "mission_metadata")
        mission.status = MissionStatus.execution
    elif current == MissionStatus.created:
        md = dict(mission.mission_metadata or {})
        md["planning_complete"] = True
        mission.mission_metadata = md
        flag_modified(mission, "mission_metadata")
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

    # Pre-execution review gate: when SOY_REVIEW_MODEL is configured,
    # require that the plan has been reviewed and passed before execution.
    from soy import config as soy_config
    if soy_config.review_enabled():
        md = dict(mission.mission_metadata or {})
        if not md.get("review_passed"):
            raise_http_error(
                status.HTTP_403_FORBIDDEN,
                "REVIEW_REQUIRED",
                "Plan review has not been completed or has not passed. "
                "Call POST /api/v1/control/missions/{id}/review-plan first.",
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

    # Commit repo_url/branch_prefix before attempting git operations
    mission.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(mission)

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
            agent_name = coder.name
        else:
            # Default to configured implementation agent (typically "droid")
            from soy import config as soy_config
            agent_name = soy_config.implementation_agent()

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
            # Sync dispatch result to MC for monitoring
            mc_sync.sync_dispatch_result(
                str(mission.id), agent_name,
                "completed" if not result.error else "error",
                result.exit_code, result.duration_seconds, result.error,
            )
        except Exception as exc:
            logger.warning(
                "Agent dispatch failed for mission %s: %s", mission_id, exc,
            )
            agent_output = {"error": str(exc)}
            mc_sync.sync_dispatch_result(
                str(mission.id), agent_name,
                "error", -1, 0, str(exc),
            )

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
        flag_modified(mission, "mission_metadata")
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
            flag_modified(mission, "mission_metadata")
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
        flag_modified(mission, "mission_metadata")
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
        flag_modified(mission, "mission_metadata")
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
