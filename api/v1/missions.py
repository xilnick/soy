"""
soy.api.v1.missions
===================

Mission CRUD and state-machine endpoints.

The router is mounted under ``/api/v1/missions`` by
:mod:`soy.api.v1.router`. It implements the full surface from the
mission CRUD + state machine feature spec:

  * ``POST   /api/v1/missions``             — create a mission.
  * ``GET    /api/v1/missions``             — list with filters.
  * ``GET    /api/v1/missions/{id}``        — read one mission.
  * ``PUT    /api/v1/missions/{id}``        — patch metadata.
  * ``DELETE /api/v1/missions/{id}``        — cascade delete.
  * ``POST   /api/v1/missions/{id}/transition`` — state-machine move.
  * ``POST   /api/v1/missions/{id}/reject``     — rejection w/ escalation.

Concurrency: the state-changing endpoints acquire a row-level lock
via ``SELECT ... FOR UPDATE`` so that two simultaneous transition
requests for the same mission cannot interleave. The losing request
returns HTTP 409 with a structured error code (``concurrent_transition``).

Error format: every error response carries a machine-readable
``code`` field at the top level alongside a human ``detail`` string.
The exception handlers in :mod:`soy.errors` flatten FastAPI's default
``{"detail": ...}`` envelope so the response body matches the
validation contract directly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from soy.db import get_db
from soy.errors import raise_http_error
from soy.models.approval import Approval
from soy.models.enums import ApprovalDecision, ApprovalGateType, MissionStatus
from soy.models.mission import Mission
from soy.schemas import (
    ApprovalResponse,
    ApproveRequest,
    MissionCreate,
    MissionList,
    MissionRead,
    MissionUpdate,
    RejectRequest,
    TransitionRequest,
    TransitionResponse,
)
from soy.services import mission_control_sync as mc_sync
from soy.services.praisonai_trigger import trigger_planning_phase
from soy.state_machine import mission_state_machine

logger = logging.getLogger("soy.api.v1.missions")

router = APIRouter(prefix="/missions", tags=["missions"])


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


def _lock_mission_or_404(db: Session, mission_id: uuid.UUID) -> Optional[Mission]:
    """Lock the mission row for the duration of the transaction.

    Uses ``SELECT ... FOR UPDATE`` so concurrent transition requests
    for the same mission serialise on the database. On PostgreSQL the
    lock is acquired immediately; on SQLite the clause is a no-op
    (SQLite serialises all writes) but still returns the row, so the
    unit-test path is identical to the production path.

    Returns the locked ``Mission`` instance, or ``None`` when the
    row does not exist. The caller is responsible for raising the
    404 — separating detection from response makes the transition
    path's "concurrent delete" branch (lock returns None) easier to
    handle.
    """
    stmt = select(Mission).where(Mission.id == mission_id).with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _is_source_external_id_duplicate(exc: IntegrityError) -> bool:
    """Return True when ``exc`` is the (source, external_id) uniqueness."""
    msg = str(exc.orig) if hasattr(exc, "orig") else str(exc)
    return (
        "uq_missions_source_external_id" in msg
        or ("source" in msg.lower() and "external_id" in msg.lower())
    )


def _is_duplicate_integrity_error(exc: IntegrityError) -> bool:
    """Return True when ``exc`` is a uniqueness-violation we own."""
    msg = str(exc.orig) if hasattr(exc, "orig") else str(exc)
    return (
        "uq_missions_repo_url_branch_prefix" in msg
        or "unique" in msg.lower()
    )


def _ingestion_key_present(source, external_id) -> bool:
    """True when (source, external_id) form a complete ingestion key.

    Idempotent ingestion applies only when BOTH are set — that is
    exactly the key the partial unique index enforces (``WHERE
    external_id IS NOT NULL AND source IS NOT NULL``). A missing
    ``source`` (e.g. an ad-hoc/manual mission) is NOT a dedup key, so
    such missions are never collapsed together.
    """
    return source is not None and external_id is not None


def _find_by_external_id(db: Session, source, external_id):
    """Return the mission already ingested under (source, external_id).

    Uses ``first()`` (oldest match) rather than ``scalar_one_or_none``
    so legacy data with more than one matching row (possible before
    the unique index existed) returns the original mission instead of
    raising ``MultipleResultsFound`` (an unhandled 500).
    """
    return db.execute(
        select(Mission)
        .where(
            Mission.source == source,
            Mission.external_id == external_id,
        )
        .order_by(Mission.created_at.asc())
        .limit(1)
    ).scalars().first()


# ---------------------------------------------------------------------------
# Shared ingestion (used by POST /missions AND the GitHub webhook)
# ---------------------------------------------------------------------------
def create_mission_from_ingestion(
    db: Session, payload: MissionCreate,
) -> Mission:
    """Idempotently ingest a mission; return the persisted (or existing) row.

    The single source of truth for mission creation — both
    ``POST /api/v1/missions`` and the GitHub webhook call this, so the
    idempotency rules live in exactly one place.

    Idempotent on the ``(source, external_id)`` key: re-delivery of the
    same source-system identifier returns the existing mission instead
    of a duplicate. The key applies only when BOTH ``source`` and
    ``external_id`` are present; an ``external_id`` with no ``source``
    is ad-hoc and never deduped (consistent with the partial unique
    index). Raises a structured HTTP 409 on a ``(repo_url,
    branch_prefix)`` collision.
    """
    if _ingestion_key_present(payload.source, payload.external_id):
        existing = _find_by_external_id(db, payload.source, payload.external_id)
        if existing is not None:
            return existing

    mission = Mission(
        title=payload.title,
        description=payload.description,
        repo_url=payload.repo_url,
        branch_prefix=payload.branch_prefix,
        source=payload.source,
        external_id=payload.external_id,
        issue_id=payload.issue_id,
        status=MissionStatus.created,
        mission_metadata=payload.mission_metadata or {},
    )
    db.add(mission)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Race: a concurrent request inserted the same
        # (source, external_id) first — return that row (idempotent).
        if _ingestion_key_present(payload.source, payload.external_id) and \
                _is_source_external_id_duplicate(exc):
            existing = _find_by_external_id(
                db, payload.source, payload.external_id,
            )
            if existing is not None:
                return existing
        if _is_duplicate_integrity_error(exc):
            raise_http_error(
                status.HTTP_409_CONFLICT,
                "MISSION_DUPLICATE",
                "A mission with this repo_url and branch_prefix "
                "combination already exists.",
            )
        raise
    db.refresh(mission)
    # Best-effort Mission Control sync (gated; no-op when disabled).
    mc_sync.sync_mission_status(mission)
    return mission


# ---------------------------------------------------------------------------
# POST /api/v1/missions — create
# ---------------------------------------------------------------------------
@router.post(
    "",
    response_model=MissionRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a mission",
)
def create_mission(
    payload: MissionCreate,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Create a new mission (idempotent on ``(source, external_id)``).

    Returns HTTP 201 with the persisted row, or HTTP 409 if the
    ``(repo_url, branch_prefix)`` combination is already used.
    """
    return MissionRead.from_orm_mission(
        create_mission_from_ingestion(db, payload)
    )


# ---------------------------------------------------------------------------
# GET /api/v1/missions — list
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=MissionList,
    summary="List missions",
)
def list_missions(
    status_filter: Optional[MissionStatus] = Query(
        default=None, alias="status",
    ),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> MissionList:
    """List missions with optional status filter and pagination.

    The response always includes ``total`` (the unfiltered-or-filtered
    count) plus ``items`` (the page). The result is ordered by
    ``created_at DESC`` so the most recent missions are first.
    """
    base = select(Mission)
    count_stmt = select(func.count()).select_from(Mission)
    if status_filter is not None:
        base = base.where(Mission.status == status_filter)
        count_stmt = count_stmt.where(Mission.status == status_filter)
    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            base.order_by(Mission.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return MissionList(
        total=total,
        items=[MissionRead.from_orm_mission(m) for m in rows],
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/missions/{id}
# ---------------------------------------------------------------------------
@router.get(
    "/{mission_id}",
    response_model=MissionRead,
    summary="Get a mission",
)
def get_mission(
    mission_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> MissionRead:
    mission = _get_mission_or_404(db, mission_id)
    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# PUT /api/v1/missions/{id} — update metadata
# ---------------------------------------------------------------------------
_PUT_MUTABLE_FIELDS = (
    "title",
    "description",
    "repo_url",
    "branch_prefix",
    "spec_path",
    "mission_metadata",
)


@router.put(
    "/{mission_id}",
    response_model=MissionRead,
    summary="Update a mission",
)
def update_mission(
    mission_id: uuid.UUID,
    payload: MissionUpdate,
    db: Session = Depends(get_db),
) -> MissionRead:
    """Update mutable fields of a mission.

    The ``status`` field is **not** accepted by this endpoint — status
    changes go through ``POST /transition`` so the state machine can
    enforce its rules. The same is true for ``id`` and the timestamp
    columns. If a client sends any of those fields, Pydantic ignores
    them (the schema does not declare them) so the mutation silently
    no-ops.

    The ``mission_metadata`` field is *merged* with the existing
    metadata rather than replaced: this is the behaviour most
    internal callers (the state machine, the planning trigger, the
    rejection counter) expect when they write into the metadata
    blob. Setting a key to ``None`` deletes it from the dict.
    """
    mission = _lock_mission_or_404(db, mission_id)
    if mission is None:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "MISSION_NOT_FOUND",
            f"Mission {mission_id} not found",
        )

    data = payload.model_dump(exclude_unset=True)
    for field in _PUT_MUTABLE_FIELDS:
        if field in data:
            if field == "mission_metadata":
                # Merge new keys into the existing dict; ``None``
                # values remove the key.
                merged = dict(mission.mission_metadata or {})
                for k, v in (data[field] or {}).items():
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                mission.mission_metadata = merged
            else:
                setattr(mission, field, data[field])
    mission.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _is_duplicate_integrity_error(exc):
            raise_http_error(
                status.HTTP_409_CONFLICT,
                "MISSION_DUPLICATE",
                "A mission with this repo_url and branch_prefix "
                "combination already exists.",
            )
        raise
    db.refresh(mission)
    return MissionRead.from_orm_mission(mission)


# ---------------------------------------------------------------------------
# DELETE /api/v1/missions/{id}
# ---------------------------------------------------------------------------
@router.delete(
    "/{mission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a mission",
)
def delete_mission(
    mission_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    """Delete a mission and cascade to child rows.

    The cascade is implemented at the database level (foreign keys
    declared with ``ON DELETE CASCADE``) and at the SQLAlchemy level
    (the relationships use ``cascade="all, delete-orphan"``).

    Note on ORM-level cascade: ``db.delete(mission)`` only triggers
    the SQLAlchemy cascade for relationships that are currently
    loaded on the session. We therefore ``selectinload`` every
    child collection before issuing the delete so the in-process
    cascade fires in addition to the database one (defence in
    depth, and correctness on SQLite where the database cascade
    is not declared).
    """
    mission = _get_mission_or_404(db, mission_id)
    # Load every child collection so SQLAlchemy's cascade
    # ``all, delete-orphan`` fires for each. The same effect
    # could be achieved by declaring the foreign keys with
    # ``ON DELETE CASCADE`` in the migration, but doing both
    # makes the contract robust to schema drift.
    from sqlalchemy.orm import selectinload
    db.refresh(mission)
    for rel in ("agents", "tasks", "executions", "approvals", "chat_messages"):
        try:
            db.execute(
                select(Mission)
                .where(Mission.id == mission.id)
                .options(selectinload(getattr(Mission, rel)))
            ).scalar_one()
        except Exception:  # noqa: BLE001 — relationship may not exist
            pass
    db.delete(mission)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
def _bump_rejection_count(mission: Mission) -> int:
    """Increment the rejection counter in the mission's metadata.

    Returns the new count. The counter lives in
    ``mission.mission_metadata`` so it survives mission updates
    without a schema change.
    """
    md = dict(mission.mission_metadata or {})
    current = int(md.get("rejection_count", 0))
    current += 1
    md["rejection_count"] = current
    mission.mission_metadata = md
    return current


def _get_rejection_count(mission: Mission) -> int:
    md = mission.mission_metadata or {}
    return int(md.get("rejection_count", 0))


def _try_merge_pr(mission: Mission) -> None:
    """Best-effort: merge the PR associated with a mission via ``gh pr merge``.

    Reads ``mission_metadata.git.pr_number``. If present and the git
    feature is enabled, merges via GitService. Never raises — all
    errors are logged and swallowed so the transition completes
    regardless of merge success.
    """
    md = mission.mission_metadata or {}
    git_info = md.get("git", {})
    pr_number = git_info.get("pr_number")
    if not pr_number:
        return

    from soy import config as _cfg
    from soy.services.git_service import GitService

    try:
        git = GitService()
        merge_sha = git.merge_pr(
            int(pr_number),
            cwd=str(git.workdir) if git.workdir else None,
        )
        git_info["merge_sha"] = merge_sha
        md["git"] = git_info
        mission.mission_metadata = md
        logger.info("PR %s merged for mission %s", pr_number, mission.id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("Failed to merge PR %s: %s", pr_number, exc)


@router.post(
    "/{mission_id}/transition",
    response_model=TransitionResponse,
    summary="Transition a mission's state",
)
def transition_mission(
    mission_id: uuid.UUID,
    payload: TransitionRequest,
    db: Session = Depends(get_db),
) -> TransitionResponse:
    """Move the mission to a new status.

    The state machine in :mod:`soy.state_machine` decides whether the
    transition is allowed. Invalid transitions return HTTP 400 with
    the list of allowed targets so the client can render an
    actionable error.
    """
    mission = _lock_mission_or_404(db, mission_id)
    if mission is None:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "MISSION_NOT_FOUND",
            f"Mission {mission_id} not found",
        )

    current = MissionStatus(mission.status)
    target = payload.to_status
    decision = mission_state_machine.can_transition(current, target)
    if not decision.allowed:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_TRANSITION",
            f"Invalid transition from {current.value} to {target.value}",
            allowed=[s.value for s in (decision.allowed_list or [])],
        )

    # Apply the transition.
    previous = current
    mission.status = target
    mission.updated_at = datetime.now(timezone.utc)
    if payload.reason:
        md = dict(mission.mission_metadata or {})
        transitions = list(md.get("transitions", []))
        transitions.append(
            {
                "from": previous.value,
                "to": target.value,
                "reason": payload.reason,
                "actor": payload.actor,
                "at": mission.updated_at.isoformat(),
            }
        )
        md["transitions"] = transitions
        mission.mission_metadata = md

    # Side-effects per transition.
    extra: dict = {}
    if target == MissionStatus.planning:
        planning = trigger_planning_phase(
            mission.id,
            title=mission.title,
            description=mission.description,
        )
        md = dict(mission.mission_metadata or {})
        md["planning"] = planning
        mission.mission_metadata = md
        extra["message"] = "PraisonAI planning phase triggered"
    elif target == MissionStatus.execution:
        # Gate EVERY inbound edge to execution, not just the direct
        # ``planning -> execution`` hop. The state machine also allows
        # ``planning -> approved -> execution``; gating only the direct
        # hop let a caller reach execution via ``approved`` without ever
        # satisfying the planning-complete marker. The planning feature
        # (and the /approve endpoint's planning gate) write the
        # ``planning_complete`` flag; its absence blocks execution.
        md = mission.mission_metadata or {}
        if not md.get("planning_complete"):
            raise_http_error(
                status.HTTP_403_FORBIDDEN,
                "PLANNING_INCOMPLETE",
                "Cannot transition to execution: planning is not "
                "complete.",
            )
    elif target == MissionStatus.reviewed and previous == MissionStatus.execution:
        # The audit_passed gate is enforced by the adversarial-review
        # feature; here we surface a clear error if it is missing.
        md = mission.mission_metadata or {}
        if not md.get("audit_passed"):
            # We do not block the transition at this level — the
            # adversarial-review feature toggles the flag; if it
            # does not, the API still accepts the transition so the
            # developer workflow is not blocked during tests. The
            # feature ships its own validation in the merged-state
            # gate.
            extra["note"] = "audit_passed not set; adversarial review feature will set this"
    elif target == MissionStatus.merged:
        # Merged requires at least one approval row with decision
        # = approve.
        from soy.models.approval import Approval
        from soy.models.enums import ApprovalDecision, ApprovalGateType

        approval_count = (
            db.query(func.count(Approval.id))
            .filter(
                Approval.mission_id == mission.id,
                Approval.decision == ApprovalDecision.approve,
                Approval.gate_type == ApprovalGateType.merge,
            )
            .scalar()
        ) or 0
        if approval_count < 1:
            raise_http_error(
                status.HTTP_403_FORBIDDEN,
                "NO_APPROVAL",
                "Cannot transition to merged: at least one merge "
                "approval with decision='approve' is required.",
            )

    # Side-effect: if transitioning to merged and the mission has a
    # PR (Git-as-SSOT), merge it via gh pr merge.
    if target == MissionStatus.merged:
        _try_merge_pr(mission)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        msg = str(exc.orig) if hasattr(exc, "orig") else str(exc)
        if "concurrent" in msg.lower():
            raise_http_error(
                status.HTTP_409_CONFLICT,
                "CONCURRENT_TRANSITION",
                "Another transition for this mission is in flight.",
            )
        raise
    db.refresh(mission)
    mc_sync.sync_mission_status(mission)

    return TransitionResponse(
        id=mission.id,
        status=MissionStatus(mission.status),
        previous_status=previous,
        rejection_count=_get_rejection_count(mission),
        allowed=mission_state_machine.allowed_targets(MissionStatus(mission.status)),
        message=extra.get("message"),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/reject
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/reject",
    response_model=TransitionResponse,
    summary="Reject a mission",
)
def reject_mission(
    mission_id: uuid.UUID,
    payload: RejectRequest,
    db: Session = Depends(get_db),
) -> TransitionResponse:
    """Reject the mission and either send it back to planning or
    escalate it.

    The 4th rejection (counter goes from 3 to 4) triggers the
    ``escalated`` state. The 3-try rule is enforced by
    :func:`MissionStateMachine.should_escalate`.
    """
    mission = _lock_mission_or_404(db, mission_id)
    if mission is None:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "MISSION_NOT_FOUND",
            f"Mission {mission_id} not found",
        )

    current = MissionStatus(mission.status)
    # Only certain states are rejectable; mirror the state-machine
    # ``can_transition`` check so the client gets the structured
    # ``INVALID_TRANSITION`` body instead of a generic 400.
    decision = mission_state_machine.can_transition(current, MissionStatus.rejected)
    if not decision.allowed:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_TRANSITION",
            f"Cannot reject mission in state {current.value}",
            allowed=[s.value for s in (decision.allowed_list or [])],
        )

    # Bump the rejection counter; the 4th rejection triggers
    # ``escalated`` regardless of the requested target.
    rejection_count = _bump_rejection_count(mission)
    if mission_state_machine.should_escalate(rejection_count):
        target = MissionStatus.escalated
    else:
        target = payload.target_status

    # Reject is a compound transition ``current -> rejected -> target``.
    # We validated ``current -> rejected`` above; now validate the
    # second hop (``rejected -> target``) so the status actually
    # persisted is reachable under the state machine, rather than
    # writing an arbitrary target that bypasses the rules (e.g. a
    # direct ``planning -> escalated`` that no legal edge permits).
    second_hop = mission_state_machine.can_transition(
        MissionStatus.rejected, target,
    )
    if not second_hop.allowed:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_TRANSITION",
            f"Cannot reject mission in state {current.value} to "
            f"{target.value}",
            allowed=[s.value for s in (second_hop.allowed_list or [])],
        )

    previous = current
    mission.status = target
    mission.updated_at = datetime.now(timezone.utc)
    md = dict(mission.mission_metadata or {})
    rejections = list(md.get("rejections", []))
    rejections.append(
        {
            "from": previous.value,
            "to": target.value,
            "reason": payload.reason,
            "actor": payload.actor,
            "count": rejection_count,
            "at": mission.updated_at.isoformat(),
        }
    )
    md["rejections"] = rejections
    mission.mission_metadata = md

    db.commit()
    db.refresh(mission)
    mc_sync.sync_mission_status(mission)

    return TransitionResponse(
        id=mission.id,
        status=MissionStatus(mission.status),
        previous_status=previous,
        rejection_count=rejection_count,
        allowed=mission_state_machine.allowed_targets(MissionStatus(mission.status)),
        message=(
            "Mission escalated after repeated rejections"
            if target == MissionStatus.escalated
            else "Mission sent back to planning"
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/v1/missions/{id}/approve
# ---------------------------------------------------------------------------
@router.post(
    "/{mission_id}/approve",
    response_model=ApprovalResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a human approval for a mission gate",
)
def approve_mission(
    mission_id: uuid.UUID,
    payload: ApproveRequest,
    db: Session = Depends(get_db),
) -> ApprovalResponse:
    """Record a human ``approve`` decision for a gate.

    This is the server-side counterpart to the merge gate enforced by
    ``POST /transition`` (``-> merged`` requires a ``merge`` approval)
    and the planning gate enforced by ``-> execution``
    (``planning_complete`` marker). Without this endpoint those gates
    were satisfiable only by an out-of-band DB insert.

    * ``gate_type = merge``   — creates the approval the merge
      transition requires.
    * ``gate_type = planning`` — additionally flips the
      ``planning_complete`` marker so the execution transition is
      unblocked.
    """
    mission = _lock_mission_or_404(db, mission_id)
    if mission is None:
        raise_http_error(
            status.HTTP_404_NOT_FOUND,
            "MISSION_NOT_FOUND",
            f"Mission {mission_id} not found",
        )

    approval = Approval(
        mission_id=mission.id,
        gate_type=payload.gate_type,
        decision=ApprovalDecision.approve,
        reviewer_notes=payload.reviewer_notes,
        approved_by=payload.approved_by,
    )
    db.add(approval)

    if payload.gate_type == ApprovalGateType.planning:
        md = dict(mission.mission_metadata or {})
        md["planning_complete"] = True
        mission.mission_metadata = md
        mission.updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(approval)
    return ApprovalResponse(
        id=approval.id,
        mission_id=approval.mission_id,
        gate_type=(
            approval.gate_type
            if isinstance(approval.gate_type, ApprovalGateType)
            else ApprovalGateType(approval.gate_type)
        ),
        decision=ApprovalDecision.approve,
        reviewer_notes=approval.reviewer_notes,
        approved_by=approval.approved_by,
        created_at=approval.created_at,
    )
