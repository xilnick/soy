"""
soy.schemas
============

Pydantic request/response schemas for the SOY API.

These models are intentionally separate from the SQLAlchemy ORM
models in :mod:`soy.models`. The ORM models describe the persisted
shape; the schemas describe the wire format. Keeping them apart lets
us evolve the database schema independently from the public API
contract (e.g., rename an internal column without breaking clients).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from soy.security import validate_branch_name, validate_repo_url

from soy.models.enums import (
    AgentRole,
    AgentStatus,
    ApprovalDecision,
    ApprovalGateType,
    ExecutionStatus,
    MissionStatus,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Mission
# ---------------------------------------------------------------------------
class MissionCreate(BaseModel):
    """Request body for ``POST /api/v1/missions``.

    Required: ``title``. ``repo_url`` and ``branch_prefix`` are
    optional — dashboard-first missions do not need a repo until
    execution begins. The combination ``(repo_url, branch_prefix)``
    must be unique when both are set; a duplicate insert returns
    HTTP 409. ``description``, ``source``, ``external_id``,
    ``issue_id``, and ``mission_metadata`` are also optional.
    """

    title: str = Field(..., min_length=1, max_length=512)
    repo_url: Optional[str] = Field(default=None, max_length=512)
    branch_prefix: Optional[str] = Field(default=None, max_length=128)
    description: Optional[str] = Field(default=None, max_length=10_000)
    source: Optional[str] = Field(default=None, max_length=64)
    external_id: Optional[str] = Field(default=None, max_length=128)
    issue_id: Optional[str] = Field(default=None, max_length=64)
    mission_metadata: Optional[dict] = Field(default=None)

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_repo_url(stripped)

    @field_validator("branch_prefix")
    @classmethod
    def _validate_branch_prefix(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_branch_name(stripped)


class MissionUpdate(BaseModel):
    """Request body for ``PUT /api/v1/missions/{id}``.

    The PUT endpoint deliberately does not accept the ``status``
    field — status changes are gated through the transition endpoint
    so the state machine can enforce its rules. The same applies to
    the primary key and the timestamp columns.
    """

    title: Optional[str] = Field(default=None, min_length=1, max_length=512)
    description: Optional[str] = Field(default=None, max_length=10_000)
    repo_url: Optional[str] = Field(default=None, max_length=512)
    branch_prefix: Optional[str] = Field(default=None, max_length=128)
    spec_path: Optional[str] = Field(default=None, max_length=512)
    mission_metadata: Optional[dict] = Field(default=None)

    @field_validator("repo_url")
    @classmethod
    def _strip_blank_repo(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_repo_url(stripped)

    @field_validator("branch_prefix")
    @classmethod
    def _strip_blank_branch(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_branch_name(stripped)


class MissionRead(BaseModel):
    """Response model for mission reads.

    ``from_attributes`` is deliberately NOT enabled: the JSONB column
    is exposed under the alias ``metadata`` (the ORM attribute is
    ``mission_metadata`` to avoid clashing with SQLAlchemy's reserved
    ``Base.metadata``). With ``from_attributes`` a raw ORM Mission
    validated under this model would resolve the ``metadata`` alias via
    ``getattr`` to the SQLAlchemy ``MetaData`` registry object, not the
    JSON column — a footgun. Every endpoint builds this model via the
    explicit :meth:`from_orm_mission` (which validates a plain dict
    with the correct ``metadata`` key), so attribute validation is
    never needed.
    """

    id: uuid.UUID
    title: str
    description: Optional[str] = None
    status: MissionStatus
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    branch_prefix: Optional[str] = None
    issue_id: Optional[str] = None
    spec_path: Optional[str] = None
    source: Optional[str] = None
    external_id: Optional[str] = None
    spec_commit_sha: Optional[str] = None
    merge_commit_sha: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    mission_metadata: Optional[dict] = Field(default=None, alias="metadata")

    @classmethod
    def from_orm_mission(cls, mission: Any) -> "MissionRead":
        """Build a response from an ORM instance.

        We use a custom constructor rather than ``from_attributes``
        alone because the JSONB column is exposed under the
        ``metadata`` name in the API (the Python attribute is
        ``mission_metadata`` to avoid clashing with SQLAlchemy's
        reserved ``metadata`` attribute).
        """
        return cls.model_validate(
            {
                "id": mission.id,
                "title": mission.title,
                "description": mission.description,
                "status": mission.status
                if isinstance(mission.status, MissionStatus)
                else MissionStatus(mission.status),
                "repo_url": mission.repo_url,
                "branch": mission.branch,
                "branch_prefix": mission.branch_prefix,
                "issue_id": mission.issue_id,
                "spec_path": mission.spec_path,
                "source": mission.source,
                "external_id": mission.external_id,
                "spec_commit_sha": mission.spec_commit_sha,
                "merge_commit_sha": mission.merge_commit_sha,
                "created_at": mission.created_at,
                "updated_at": mission.updated_at,
                "metadata": getattr(mission, "mission_metadata", None),
            }
        )


class MissionList(BaseModel):
    """Paginated list response for ``GET /api/v1/missions``."""

    total: int
    items: List[MissionRead]
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
class TransitionRequest(BaseModel):
    """Request body for ``POST /api/v1/missions/{id}/transition``."""

    to_status: MissionStatus
    reason: Optional[str] = Field(default=None, max_length=2000)
    actor: Optional[str] = Field(default=None, max_length=128)


class TransitionResponse(BaseModel):
    """Response body for transition and rejection endpoints.

    ``allowed`` is populated when the transition is rejected so the
    client can render a UI hint without re-querying the state machine.
    """

    id: uuid.UUID
    status: MissionStatus
    previous_status: MissionStatus
    rejection_count: int
    allowed: Optional[List[MissionStatus]] = None
    message: Optional[str] = None


class RejectRequest(BaseModel):
    """Request body for ``POST /api/v1/missions/{id}/reject``."""

    target_status: MissionStatus = Field(default=MissionStatus.planning)
    reason: Optional[str] = Field(default=None, max_length=2000)
    actor: Optional[str] = Field(default=None, max_length=128)

    @field_validator("target_status")
    @classmethod
    def _must_be_reachable(cls, value: MissionStatus) -> MissionStatus:
        """A rejection may only send a mission back to ``planning`` or
        straight to ``escalated`` (the 4th rejection does the latter
        automatically; this validator rejects anything else)."""
        if value not in (MissionStatus.planning, MissionStatus.escalated):
            raise ValueError(
                "target_status must be 'planning' or 'escalated'"
            )
        return value


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------
class ApproveRequest(BaseModel):
    """Request body for ``POST /api/v1/missions/{id}/approve``.

    Records a human ``approve`` decision for a gate. ``gate_type``
    defaults to ``merge`` (the final pre-merge audit); ``planning``
    records sign-off on the RFC/plan and also flips the
    ``planning_complete`` marker that gates the execution transition.
    """

    gate_type: ApprovalGateType = Field(default=ApprovalGateType.merge)
    reviewer_notes: Optional[str] = Field(default=None, max_length=20_000)
    approved_by: Optional[str] = Field(default=None, max_length=128)


class ApprovalResponse(BaseModel):
    """Response body for the approve endpoint (the created row)."""

    id: uuid.UUID
    mission_id: uuid.UUID
    gate_type: ApprovalGateType
    decision: ApprovalDecision
    reviewer_notes: Optional[str] = None
    approved_by: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AgentCreate(BaseModel):
    """Request body for ``POST /api/v1/missions/{id}/agents``.

    Required: ``name``, ``role``. The ``role`` field is restricted
    to the four known enum values (``coder``, ``qa``, ``reviewer``,
    ``orchestrator``) by the Pydantic enum coercion; a request with
    an unknown role returns 422.

    The ``sandbox`` flag toggles the agent's tool list at execution
    time. When ``True`` (the safe default) the agent receives only
    ``file_read`` and ``file_write``; when ``False`` it also receives
    ``run_command`` and ``web_search``.

    ``system_prompt`` is optional; when set it overrides the default
    prompt PraisonAI generates from the role/goal/backstory triplet.
    """

    name: str = Field(..., min_length=1, max_length=128)
    role: AgentRole
    model: Optional[str] = Field(default=None, max_length=128)
    llm_config: Optional[dict] = Field(default=None)
    sandbox: bool = True
    system_prompt: Optional[str] = Field(default=None, max_length=20_000)
    tool_config: Optional[dict] = Field(default=None)


class AgentRead(BaseModel):
    """Response body for agent reads."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mission_id: uuid.UUID
    name: str
    role: AgentRole
    model: Optional[str] = None
    llm_config: Optional[dict] = None
    status: AgentStatus
    sandbox: bool = True
    tool_config: Optional[dict] = None
    system_prompt: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_agent(cls, agent: Any) -> "AgentRead":
        return cls.model_validate(
            {
                "id": agent.id,
                "mission_id": agent.mission_id,
                "name": agent.name,
                "role": agent.role
                if isinstance(agent.role, AgentRole)
                else AgentRole(agent.role),
                "model": agent.model,
                "llm_config": agent.llm_config,
                "status": agent.status
                if isinstance(agent.status, AgentStatus)
                else AgentStatus(agent.status),
                "sandbox": bool(getattr(agent, "sandbox", True)),
                "tool_config": agent.tool_config,
                "system_prompt": agent.system_prompt,
                "created_at": agent.created_at,
                "updated_at": agent.updated_at,
            }
        )


class AgentList(BaseModel):
    """Paginated list response for ``GET /api/v1/missions/{id}/agents``."""

    total: int
    items: List[AgentRead]


class AgentTeamResponse(BaseModel):
    """Response body for ``POST /api/v1/missions/{id}/agents/team``.

    The team is returned in the canonical order
    ``orchestrator → coder → qa → reviewer`` (with missing roles
    omitted) so the caller can verify the assembly order without
    re-deriving it.
    """

    mission_id: uuid.UUID
    team: List[AgentRead]
    order: List[str]
    parallel_supported: bool = True
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------
class TaskCreate(BaseModel):
    """Request body for ``POST /api/v1/missions/{id}/agents/{aid}/tasks``.

    Required: ``description``. ``expected_output``, ``depends_on``,
    and ``config`` are optional. ``depends_on`` is a JSON list of
    upstream task UUIDs the worker waits for before enqueuing the
    task. ``config`` is a free-form JSONB blob (e.g. per-task
    timeout, retry policy overrides).
    """

    description: str = Field(..., min_length=1, max_length=50_000)
    expected_output: Optional[str] = Field(default=None, max_length=10_000)
    depends_on: Optional[List[uuid.UUID]] = Field(default=None)
    config: Optional[dict] = Field(default=None)


class TaskRead(BaseModel):
    """Response body for task reads."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    mission_id: uuid.UUID
    agent_id: uuid.UUID
    description: str
    expected_output: Optional[str] = None
    status: TaskStatus
    depends_on: Optional[List[uuid.UUID]] = None
    attempt_count: int
    config: Optional[dict] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_task(cls, task: Any) -> "TaskRead":
        return cls.model_validate(
            {
                "id": task.id,
                "mission_id": task.mission_id,
                "agent_id": task.agent_id,
                "description": task.description,
                "expected_output": task.expected_output,
                "status": task.status
                if isinstance(task.status, TaskStatus)
                else TaskStatus(task.status),
                "depends_on": task.depends_on,
                "attempt_count": task.attempt_count,
                "config": task.config,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        )


class TaskList(BaseModel):
    """Paginated list response for ``GET /api/v1/missions/{id}/tasks``."""

    total: int
    items: List[TaskRead]


class TaskExecuteRequest(BaseModel):
    """Request body for ``POST /api/v1/missions/{id}/tasks/{tid}/execute``.

    ``parallel`` toggles the workflow's ``process`` mode. When
    ``True`` independent tasks (no shared dependencies) run in
    parallel. When ``False`` (the default for safety) the worker
    runs the single task in isolation against the agent assigned
    to it.
    """

    parallel: bool = False
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=3_600)


class TaskExecuteResponse(BaseModel):
    """Response body for the execute endpoint.

    On success the response carries the latest execution row
    (``execution_id``) and the new task status. On failure the
    response carries the same plus a ``retry_scheduled`` boolean
    and (when the 3-try rule fires) an ``escalated`` flag.
    """

    task_id: uuid.UUID
    status: TaskStatus
    execution_id: Optional[uuid.UUID] = None
    attempt_number: Optional[int] = None
    output: Optional[dict] = None
    error: Optional[str] = None
    retry_scheduled: bool = False
    escalated: bool = False
    attempt_count: int = 0
    message: Optional[str] = None


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
class ExecutionRead(BaseModel):
    """Response body for execution reads."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    agent_id: uuid.UUID
    mission_id: uuid.UUID
    status: ExecutionStatus
    attempt_number: int
    output: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_execution(cls, execution: Any) -> "ExecutionRead":
        return cls.model_validate(
            {
                "id": execution.id,
                "task_id": execution.task_id,
                "agent_id": execution.agent_id,
                "mission_id": execution.mission_id,
                "status": execution.status
                if isinstance(execution.status, ExecutionStatus)
                else ExecutionStatus(execution.status),
                "attempt_number": execution.attempt_number,
                "output": execution.output,
                "error": execution.error,
                "started_at": execution.started_at,
                "finished_at": execution.finished_at,
                "created_at": execution.created_at,
                "updated_at": execution.updated_at,
            }
        )


class ExecutionList(BaseModel):
    """List response for execution log endpoints."""

    total: int
    items: List[ExecutionRead]


# ---------------------------------------------------------------------------
# Mission logs (unified, chronological view)
# ---------------------------------------------------------------------------
class LogEntry(BaseModel):
    """A single line in a mission's unified log.

    ``kind`` is ``execution`` (the source of truth — a row in the
    ``executions`` table) or a supplementary lifecycle event derived
    from the mission metadata (``transition`` / ``rejection``).
    """

    timestamp: str  # ISO-8601; sorts chronologically as a string
    kind: str
    level: str  # info | warning | error
    message: str
    detail: Optional[dict] = None


class MissionLogResponse(BaseModel):
    """Paginated unified log for ``GET /api/v1/missions/{id}/logs``."""

    mission_id: uuid.UUID
    total: int
    entries: List[LogEntry]


# ---------------------------------------------------------------------------
# Control dashboard
# ---------------------------------------------------------------------------
class ControlMissionCreate(BaseModel):
    """Request body for ``POST /api/v1/control/missions``.

    Minimal dashboard-first creation — only title is required.
    ``description`` and ``source`` are optional. No repo_url or
    branch_prefix needed for the control dashboard.
    """

    title: str = Field(..., min_length=1, max_length=512)
    description: Optional[str] = Field(default=None, max_length=10_000)
    source: Optional[str] = Field(default="dashboard", max_length=64)
    mission_metadata: Optional[dict] = Field(default=None)


class RefineRequest(BaseModel):
    """Request body for ``POST /api/v1/control/missions/{id}/refine``.

    Send a refinement prompt to the mission's orchestrator agent.
    The agent iterates on the mission description, improving clarity,
    scope, or technical details.
    """

    prompt: Optional[str] = Field(
        default=None,
        max_length=20_000,
        description="Optional refinement instructions. "
                    "When None, the agent refines based on existing description.",
    )
    model: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Override model for the refinement agent.",
    )
    timeout_seconds: Optional[int] = Field(default=300, ge=1, le=3600)


class ResearchRequest(BaseModel):
    """Request body for ``POST /api/v1/control/missions/{id}/research``.

    Dispatches a research agent (hermes by default) for the mission,
    plus an optional DeerFlow deep-research task.
    Results are stored in the mission's metadata under ``research``.
    """

    query: Optional[str] = Field(
        default=None,
        max_length=20_000,
        description="Research query. Defaults to mission title + description.",
    )
    agent: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Override the research agent (default: hermes).",
    )
    model: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Override the LLM model for the research agent.",
    )
    timeout_seconds: Optional[int] = Field(default=300, ge=1, le=3600)


class VerifyRequest(BaseModel):
    """Request body for ``POST /api/v1/control/missions/{id}/verify``.

    Dispatches the QA/reviewer agent to verify the mission plan.
    Stores verification result in mission metadata under ``verification``.
    """

    prompt: Optional[str] = Field(
        default=None,
        max_length=20_000,
        description="Verification instructions. "
                    "Defaults to checking plan completeness and feasibility.",
    )
    model: Optional[str] = Field(default=None, max_length=128)
    timeout_seconds: Optional[int] = Field(default=300, ge=1, le=3600)


class ReviewPlanRequest(BaseModel):
    """Request body for ``POST /api/v1/control/missions/{id}/review-plan``.

    Optional pre-execution review of the mission plan using a dedicated
    review model (e.g. z-ai/glm-5.2). Gated by SOY_REVIEW_MODEL.
    """

    model: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Override the review model (default: SOY_REVIEW_MODEL).",
    )
    prompt: Optional[str] = Field(
        default=None,
        max_length=20_000,
        description="Custom review instructions. Defaults to plan completeness check.",
    )
    timeout_seconds: Optional[int] = Field(default=300, ge=1, le=3600)


class AutoRunRequest(BaseModel):
    """Request body for ``POST /api/v1/control/missions/{id}/auto-run``.

    Full autonomous run: branch → dispatch agent → commit → merge.
    Requires that mission has a repo_url set (either at creation or
    via PUT /missions/{id}).
    """

    repo_url: Optional[str] = Field(
        default=None,
        max_length=512,
        description="Set repo_url on the mission if not already set.",
    )
    branch_prefix: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Override branch prefix (default: feature/soy-{id}).",
    )
    agent_name: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Coding agent to use (default: use mission's coder agent).",
    )
    prompt: Optional[str] = Field(
        default=None,
        max_length=50_000,
        description="Override prompt for the coding agent.",
    )
    auto_merge: bool = Field(
        default=True,
        description="When True, auto-merge the branch after agent completes.",
    )
    timeout_seconds: Optional[int] = Field(default=600, ge=1, le=7200)

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_repo_url(stripped)

    @field_validator("branch_prefix")
    @classmethod
    def _validate_branch_prefix(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        return validate_branch_name(stripped)


class MergeRequest(BaseModel):
    """Request body for ``POST /api/v1/control/missions/{id}/merge``.

    Merge the mission's feature branch into main (local backend)
    or merge the PR (remote backend).
    """

    strategy: str = Field(
        default="squash",
        max_length=16,
        description="Merge strategy: 'squash' or 'merge'.",
    )


class ControlStatusResponse(BaseModel):
    """Aggregated status for a mission in the control dashboard."""

    mission_id: uuid.UUID
    title: str
    status: MissionStatus
    description: Optional[str] = None
    repo_url: Optional[str] = None
    branch: Optional[str] = None
    agent_count: int = 0
    task_count: int = 0
    completed_tasks: int = 0
    git_info: Optional[dict] = None
    research_results: Optional[list] = None
    verification_results: Optional[list] = None
    refinement_history: Optional[list] = None
    last_execution: Optional[dict] = None


class AutoRunResponse(BaseModel):
    """Response for the autonomous run endpoint."""

    mission_id: uuid.UUID
    status: str
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    merged: bool = False
    merge_sha: Optional[str] = None
    agent_output: Optional[dict] = None
    error: Optional[str] = None
    message: Optional[str] = None
