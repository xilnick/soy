"""
soy.services.praisonai_worker
=============================

``SoyWorker`` — the bridge between the SOY domain layer (DB rows)
and the PraisonAI runtime (``praisonaiagents.Agent`` /
``praisonaiagents.Agents`` / ``praisonaiagents.Task``).

Responsibilities
----------------

* **Agent construction.** Each ``Agent`` row in the database is
  translated into a ``praisonaiagents.Agent`` instance. The
  worker's :func:`_build_praisonai_agent` helper centralises the
  mapping so the same logic drives single-agent task execution
  and multi-agent team assembly.

* **Tool restriction.** Agents with ``sandbox = True`` (the
  default) receive only ``file_read`` and ``file_write``; agents
  with ``sandbox = False`` also receive ``run_command`` and
  ``web_search``. The tool list is the *only* place the sandbox
  flag is enforced — there is no prompt-level guard that could
  be bypassed by the LLM.

* **Model resolution.** The worker delegates to
  :mod:`soy.services.model_resolver` so the routing rules (cloud
  vs local Ollama, API key injection) live in one place.

* **3-try retry rule.** When a PraisonAI task returns
  ``status = "failed"`` the worker reschedules a new execution
  attempt on the same task. After 3 failed attempts the worker
  marks the task ``escalated``, emits a WebSocket event, and
  escalates the parent mission.

* **Timeouts.** Each task has a configurable timeout (default
  ``TASK_TIMEOUT_SECONDS``). When the timeout fires the worker
  cancels the underlying coroutine/thread and writes
  ``status = "failed"`` with ``error = "timeout"``.

* **Parallel execution.** Independent tasks (no shared
  dependencies) can be executed in parallel by setting
  ``parallel = True`` on the execute request; the worker sets
  ``Agents(process="parallel", ...)`` accordingly.

* **Error capture.** Tool exceptions inside the agent bubble to
  the ``executions.error`` column. Malformed JSON output is
  caught at the worker boundary and converted into a
  ``status = "failed"`` execution row before the retry policy
  fires.

The worker is intentionally side-effect-only: it mutates the
database (inserts ``executions`` rows, updates ``tasks`` and
``missions``) and broadcasts WebSocket events, but does not own
HTTP routing. The router in :mod:`soy.api.v1.tasks` calls
:meth:`SoyWorker.execute_task` and serialises the result.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

import soy.db  # noqa: F401  — referenced lazily via _default_session_factory
from soy.models.agent import Agent
from soy.models.enums import (
    AgentRole,
    AgentStatus,
    ExecutionStatus,
    MissionStatus,
    TaskStatus,
)
from soy.models.execution import Execution
from soy.models.mission import Mission
from soy.models.task import Task
from soy.services.model_resolver import resolve_model

logger = logging.getLogger("soy.services.praisonai_worker")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Maximum number of attempts (including the first) before the
# task is escalated. The validation contract calls this the 3-try
# rule: 1 initial + 2 retries = 3 attempts, and the 4th failure
# triggers escalation.
MAX_ATTEMPTS = 3

# Default per-task timeout in seconds. The router may override
# this with a per-request value.
DEFAULT_TASK_TIMEOUT_SECONDS = 600

# Linear backoff between retries (attempt 1 → 2 waits 5s,
# attempt 2 → 3 waits 15s).
RETRY_BACKOFF_SECONDS = (5, 15)

# Canonical assembly order for the AgentTeam. The validation
# contract pins this order so the API can verify it without
# running the agent.
TEAM_ROLE_ORDER: Sequence[AgentRole] = (
    AgentRole.orchestrator,
    AgentRole.coder,
    AgentRole.qa,
    AgentRole.reviewer,
)

# Canonical tool lists. The sandbox flag toggles between them.
SANDBOXED_TOOLS: List[str] = ["file_read", "file_write"]
UNSANDBOXED_TOOLS: List[str] = [
    "file_read", "file_write", "run_command", "web_search",
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class TaskExecutionResult:
    """Outcome of a single task execution.

    The router serialises this into the API response (see
    :class:`soy.schemas.TaskExecuteResponse`). The fields are
    intentionally simple so the response is deterministic.
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
# Tool whitelist helper
# ---------------------------------------------------------------------------
def tools_for_sandbox(sandbox: bool) -> List[str]:
    """Return the tool list for a given sandbox flag.

    Sandboxed agents (the safe default) receive only file-level
    tools. Un-sandboxed agents also receive shell + web search.
    """
    if sandbox:
        return list(SANDBOXED_TOOLS)
    return list(UNSANDBOXED_TOOLS)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
class SoyWorker:
    """Bridge between the SOY database and the PraisonAI runtime.

    The worker is stateless across requests: every call to
    :meth:`execute_task` opens its own DB session and returns a
    :class:`TaskExecutionResult`. Tests can instantiate the class
    freely without worrying about cleanup.

    A single ``SoyWorker`` instance is shared by the FastAPI
    dependency-injection graph so the executor pool is reused
    across requests.
    """

    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Session]] = None,
        executor: Optional[ThreadPoolExecutor] = None,
        event_publisher: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ) -> None:
        # Resolve ``get_session_local`` lazily on every call so
        # tests that replace ``soy.db`` (or reset the engine
        # cache) are honoured. The previous implementation
        # captured the function at import time, which made
        # the worker insensitive to engine resets.
        def _default_session_factory() -> Session:
            import soy.db as _mod
            return _mod.get_session_local()()
        self._session_factory = session_factory or _default_session_factory
        # Eagerly resolve the sessionmaker so ``with`` works
        # against both shapes. Always look it up via the
        # module reference (not the imported name) so tests
        # that patch ``soy.db.get_session_local`` are honoured.
        if session_factory is not None:
            self._sessionmaker = session_factory
        else:
            import soy.db as _mod
            self._sessionmaker = _mod.get_session_local()
        # Two distinct pools. ``_executor`` fans out the parallel
        # task layer in :meth:`execute_mission_tasks`; each fanned-out
        # ``execute_task`` then submits its workflow to the SEPARATE
        # ``_workflow_executor``. Sharing one bounded pool for both
        # levels self-deadlocks: a layer of >= max_workers tasks fills
        # every thread with outer calls that each block waiting for an
        # inner workflow future that can never be scheduled.
        self._executor = executor or ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="soy-fanout",
        )
        self._workflow_executor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="soy-workflow",
        )
        self._event_publisher = event_publisher or self._default_publisher

    # ------------------------------------------------------------------
    # Public API — single task execution
    # ------------------------------------------------------------------
    def execute_task(
        self,
        task_id: uuid.UUID,
        *,
        timeout_seconds: Optional[int] = None,
        parallel: bool = False,
    ) -> TaskExecutionResult:
        """Execute a single task via PraisonAI, with retries.

        The method implements the 3-try rule:

        * On success, the task is marked ``completed`` and no
          further attempts are scheduled.
        * On failure (status=failed, error=exception, or
          timeout), the worker schedules a new attempt if
          ``attempt_count < MAX_ATTEMPTS``. The retry waits
          ``RETRY_BACKOFF_SECONDS[i]`` seconds.
        * After ``MAX_ATTEMPTS`` failures the worker sets the
          task status to ``escalated`` and the parent mission to
          ``escalated``, then emits a ``task.escalated`` event
          and a ``mission.escalated`` event.

        Parameters
        ----------
        task_id:
            UUID of the task to execute.
        timeout_seconds:
            Optional override for the per-task timeout. When
            ``None`` the worker uses
            :data:`DEFAULT_TASK_TIMEOUT_SECONDS`.
        parallel:
            Hint flag. When the task is part of a multi-task
            batch the worker is called by
            :meth:`execute_mission_tasks` with ``parallel=True``
            so the underlying ``Agents`` workflow uses
            ``process="parallel"``. Single-task execution ignores
            the flag.

        Returns
        -------
        :class:`TaskExecutionResult`
            The outcome of the *last* attempt (or of the first
            successful attempt, if the chain halted early).
        """
        timeout = int(timeout_seconds or DEFAULT_TASK_TIMEOUT_SECONDS)
        last_result: Optional[TaskExecutionResult] = None
        for attempt_number in range(1, MAX_ATTEMPTS + 1):
            last_result = self._execute_single_attempt(
                task_id, attempt_number=attempt_number, timeout_seconds=timeout,
                parallel=parallel,
            )
            if last_result.status == TaskStatus.completed:
                return last_result
            if not last_result.retry_scheduled:
                # Terminal failure (e.g. escalated) — exit early.
                return last_result
            # Otherwise back off before the next attempt.
            if attempt_number < MAX_ATTEMPTS:
                backoff = RETRY_BACKOFF_SECONDS[
                    min(attempt_number - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                ]
                logger.info(
                    "Task %s attempt %d failed; backing off %ds before "
                    "retry.", task_id, attempt_number, backoff,
                )
                time.sleep(backoff)
        return last_result or TaskExecutionResult(
            task_id=task_id, status=TaskStatus.failed, error="no attempt ran",
        )

    def execute_mission_tasks(
        self,
        mission_id: uuid.UUID,
        *,
        parallel: bool = True,
        timeout_seconds: Optional[int] = None,
    ) -> List[TaskExecutionResult]:
        """Execute every task in a mission, optionally in parallel.

        The method groups tasks by their ``depends_on`` graph: the
        first "layer" (tasks with no dependencies) is submitted
        in parallel, then each subsequent layer waits for the
        previous one to finish. The result is a list of
        :class:`TaskExecutionResult` in the order tasks were
        enqueued.

        When ``parallel = False`` the tasks are executed
        sequentially in dependency order.
        """
        with self._sessionmaker() as db:
            tasks = (
                db.execute(
                    select(Task).where(Task.mission_id == mission_id)
                    .order_by(Task.created_at.asc())
                )
                .scalars()
                .all()
            )
            if not tasks:
                return []
            task_ids = [t.id for t in tasks]
            # Build the dependency graph in-process.
            depends_on: Dict[uuid.UUID, List[uuid.UUID]] = {
                t.id: [uuid.UUID(str(x)) for x in (t.depends_on or [])]
                for t in tasks
            }

        task_id_set = set(task_ids)

        # Topological layering: layer 0 is tasks whose in-mission
        # dependencies have all been placed in an earlier layer. A
        # dependency UUID that is not a task in this mission
        # (external / dangling) does not block layering — it is
        # caught at execution time as an unsatisfiable dependency.
        remaining = dict(depends_on)
        layers: List[List[uuid.UUID]] = []
        seen: set[uuid.UUID] = set()
        while remaining:
            current = [
                tid for tid, deps in remaining.items()
                if all((d in seen) or (d not in task_id_set) for d in deps)
            ]
            if not current:
                # Genuine in-mission cycle — break by taking the rest
                # in one layer; the dependency gate below then marks
                # them skipped (their deps can never complete).
                current = list(remaining.keys())
            layers.append(current)
            for tid in current:
                seen.add(tid)
                remaining.pop(tid, None)

        results_by_id: Dict[uuid.UUID, TaskExecutionResult] = {}
        completed_ok: set[uuid.UUID] = set()

        def _deps_satisfied(tid: uuid.UUID) -> tuple[bool, Optional[str]]:
            """A task may run only when every dependency *completed*."""
            for dep in depends_on.get(tid, []):
                if dep not in task_id_set:
                    return False, f"dependency {dep} is not a task in this mission"
                if dep not in completed_ok:
                    return False, f"dependency {dep} did not complete"
            return True, None

        # Process layers in order. Within a layer, run the
        # dependency-satisfied tasks (concurrently when ``parallel``);
        # tasks whose dependencies failed/escalated/are missing or
        # cyclic are skipped — never executed against a missing or
        # broken upstream artifact.
        for layer in layers:
            runnable: List[uuid.UUID] = []
            for tid in layer:
                ok, reason = _deps_satisfied(tid)
                if ok:
                    runnable.append(tid)
                else:
                    results_by_id[tid] = TaskExecutionResult(
                        task_id=tid,
                        status=TaskStatus.pending,
                        retry_scheduled=False,
                        escalated=False,
                        error="dependency_not_satisfied",
                        message=f"skipped: {reason}",
                    )

            if parallel:
                futures = {
                    self._executor.submit(
                        self.execute_task, tid,
                        timeout_seconds=timeout_seconds, parallel=True,
                    ): tid
                    for tid in runnable
                }
                for fut, tid in futures.items():
                    try:
                        r = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.exception(
                            "Task %s crashed in executor: %s", tid, exc,
                        )
                        r = TaskExecutionResult(
                            task_id=tid, status=TaskStatus.failed,
                            error=str(exc),
                        )
                    results_by_id[tid] = r
                    if r.status == TaskStatus.completed:
                        completed_ok.add(tid)
            else:
                for tid in runnable:
                    r = self.execute_task(
                        tid, timeout_seconds=timeout_seconds, parallel=False,
                    )
                    results_by_id[tid] = r
                    if r.status == TaskStatus.completed:
                        completed_ok.add(tid)

        return [results_by_id[tid] for tid in task_ids if tid in results_by_id]

    # ------------------------------------------------------------------
    # AgentTeam assembly
    # ------------------------------------------------------------------
    def assemble_team(
        self, mission_id: uuid.UUID, db: Optional[Session] = None,
    ) -> tuple[List[uuid.UUID], List[str]]:
        """Assemble the ``AgentTeam`` for ``mission_id``.

        When ``db`` is provided (the recommended path) the
        operation runs inside the caller's session so it shares
        the same transaction. When ``db`` is ``None`` the
        worker opens its own session using
        ``self._session_factory`` — this path is used by the
        module-level ``assemble_team_for_mission`` convenience
        function.

        Returns the list of agent IDs in canonical assembly
        order (``orchestrator → coder → qa → reviewer``) and
        the role-name list in the same order. Roles that are
        not present in the mission are omitted, but the order
        of the present roles is preserved.
        """
        def _impl(session: Session) -> tuple[List[uuid.UUID], List[str]]:
            agents = (
                session.execute(
                    select(Agent)
                    .where(Agent.mission_id == mission_id)
                    .order_by(Agent.created_at.asc())
                )
                .scalars()
                .all()
            )
            # A mission may legitimately hold more than one agent of
            # the same role (e.g. a pair of coders). Collect *all*
            # agents per role so none is silently dropped, then emit
            # them grouped in canonical role order (creation order
            # within a role).
            by_role: Dict[AgentRole, List[Agent]] = {}
            for a in agents:
                role = a.role if isinstance(a.role, AgentRole) else AgentRole(a.role)
                by_role.setdefault(role, []).append(a)
            ordered: List[Agent] = []
            order_names: List[str] = []
            for role in TEAM_ROLE_ORDER:
                for a in by_role.get(role, []):
                    ordered.append(a)
                    order_names.append(role.value)
            return [a.id for a in ordered], order_names

        if db is not None:
            return _impl(db)
        with self._sessionmaker() as session:
            return _impl(session)

    def build_praisonai_agent(self, agent_id: uuid.UUID) -> Any:
        """Return a ``praisonaiagents.Agent`` for the given DB row.

        The method does *not* start the agent — it constructs the
        instance. Callers can submit the agent to a
        ``praisonaiagents.Agents`` workflow or invoke
        ``.start(...)`` directly.
        """
        return self._build_praisonai_agent_for_db_row(agent_id)

    # ------------------------------------------------------------------
    # Internal: single attempt
    # ------------------------------------------------------------------
    def _execute_single_attempt(
        self,
        task_id: uuid.UUID,
        *,
        attempt_number: int,
        timeout_seconds: int,
        parallel: bool,
    ) -> TaskExecutionResult:
        """Run one attempt; return the outcome (incl. retry hint)."""
        started_at = datetime.now(timezone.utc)
        execution_id = uuid.uuid4()

        # 1. Load the task + agent inside a short-lived session.
        with self._sessionmaker() as db:
            task = db.get(Task, task_id)
            if task is None:
                return TaskExecutionResult(
                    task_id=task_id, status=TaskStatus.failed,
                    error="task_not_found",
                )
            # Idempotency / terminal-state guard: a task that has
            # already reached a terminal state (completed or
            # escalated) is never re-run. Re-POSTing /execute on it
            # must not reset its state or hand it a fresh 3-try
            # budget — return the existing outcome unchanged.
            if task.status in (TaskStatus.completed, TaskStatus.escalated):
                return TaskExecutionResult(
                    task_id=task_id,
                    status=task.status,
                    attempt_number=attempt_number,
                    attempt_count=task.attempt_count or 0,
                    retry_scheduled=False,
                    escalated=(task.status == TaskStatus.escalated),
                    message=(
                        f"task already {task.status.value}; not re-executed"
                    ),
                )
            # Count this attempt against the persisted 3-try budget
            # *before* doing any work, so an agent-construction
            # failure also consumes an attempt and the escalation
            # decision keys off the durable counter rather than an
            # in-memory loop index (which would let a re-invocation
            # silently grant a brand-new budget).
            task.attempt_count = (task.attempt_count or 0) + 1
            task.status = TaskStatus.running
            db.flush()
            agent = db.get(Agent, task.agent_id)
            if agent is None:
                return self._record_execution(
                    db, task, execution_id, attempt_number, started_at,
                    status=ExecutionStatus.failed,
                    error="agent_not_found",
                )
            agent_status_db_id = agent.id
            agent_sandbox = bool(agent.sandbox)
            agent_model = agent.model
            agent_role = (
                agent.role if isinstance(agent.role, AgentRole)
                else AgentRole(agent.role)
            )
            system_prompt = agent.system_prompt
            task_description = task.description
            task_expected = task.expected_output
            db.commit()
            db.refresh(task)

        # 1b. If coding-agent dispatch is enabled AND the agent is a
        #     coder AND the mission has a git branch (Git-as-SSOT),
        #     dispatch via the coding agent CLI instead of PraisonAI.
        from soy import config as _soy_config
        if (
            _soy_config.coding_agent_enabled()
            and agent_role == AgentRole.coder
            and mission_has_git_branch(self._sessionmaker, task.mission_id)
        ):
            return self._dispatch_coding_agent(
                task_id=task_id,
                mission_id=task.mission_id,
                agent_name=agent_model,  # model column doubles as agent manifest name
                prompt=task_description,
                execution_id=execution_id,
                attempt_number=attempt_number,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
            )

        # 2. Build the PraisonAI agent and task.
        try:
            pa_agent = self._build_praisonai_agent(
                agent_id=agent_status_db_id,
                model=agent_model,
                role=agent_role,
                sandbox=agent_sandbox,
                system_prompt=system_prompt,
            )
        except Exception as exc:  # noqa: BLE001 — construction failure
            logger.exception(
                "Failed to build PraisonAI agent for task %s: %s",
                task_id, exc,
            )
            return self._record_failed_execution(
                task_id=task_id,
                execution_id=execution_id,
                attempt_number=attempt_number,
                started_at=started_at,
                error=f"agent_construction_failed: {exc}",
            )

        try:
            pa_task = self._build_praisonai_task(
                pa_agent=pa_agent,
                description=task_description,
                expected_output=task_expected,
                parallel=parallel,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Failed to build PraisonAI task for task %s: %s",
                task_id, exc,
            )
            return self._record_failed_execution(
                task_id=task_id,
                execution_id=execution_id,
                attempt_number=attempt_number,
                started_at=started_at,
                error=f"task_construction_failed: {exc}",
            )

        # 3. Submit the task to a one-agent Agents workflow.
        workflow = self._build_agents_workflow(
            pa_agent=pa_agent, pa_task=pa_task, parallel=parallel,
        )

        # 4. Run the workflow under a timeout.
        output, error, ok = self._run_with_timeout(
            workflow, pa_agent, pa_task, timeout_seconds,
        )

        finished_at = datetime.now(timezone.utc)
        if not ok:
            # Record the failed execution row inside the
            # worker's own session (the request session has
            # already been closed by the time the timeout
            # fires). The record call also bumps the task
            # status to ``pending`` (with a retry) or
            # ``escalated`` (after MAX_ATTEMPTS) and updates
            # ``task.attempt_count`` so the response reflects
            # the latest attempt.
            return self._record_failed_execution(
                task_id=task_id,
                execution_id=execution_id,
                attempt_number=attempt_number,
                started_at=started_at,
                error=error or "agent_exception",
            )

        # 5. Record the successful execution and mark the task complete.
        return self._record_successful_execution(
            task_id=task_id,
            execution_id=execution_id,
            attempt_number=attempt_number,
            started_at=started_at,
            finished_at=finished_at,
            output=output,
        )

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------
    def _build_praisonai_agent_for_db_row(self, agent_id: uuid.UUID) -> Any:
        with self._sessionmaker() as db:
            agent = db.get(Agent, agent_id)
            if agent is None:
                raise ValueError(f"agent {agent_id} not found")
            return self._build_praisonai_agent(
                agent_id=agent.id,
                model=agent.model,
                role=(
                    agent.role if isinstance(agent.role, AgentRole)
                    else AgentRole(agent.role)
                ),
                sandbox=bool(agent.sandbox),
                system_prompt=agent.system_prompt,
            )

    def _build_praisonai_agent(
        self,
        *,
        agent_id: uuid.UUID,
        model: Optional[str],
        role: AgentRole,
        sandbox: bool,
        system_prompt: Optional[str],
    ) -> Any:
        """Construct a ``praisonaiagents.Agent``.

        Imports are deferred so importing this module on a host
        that does not have ``praisonaiagents`` installed does not
        fail at import time (the planning trigger checks the
        same condition at runtime).
        """
        from praisonaiagents import Agent

        # Resolve the model. When the agent row carries no explicit
        # model we use ``SOY_MODEL`` (written by the deploy
        # blueprint), falling back to a *local* Ollama model that
        # needs no API key — never a ``:cloud`` default, which would
        # silently require (and, in older builds, persist) a cloud
        # credential for a model-less agent. Production always sets
        # SOY_MODEL, so this literal is a safe last-resort only.
        model_str = model or os.environ.get("SOY_MODEL") or "ollama/llama3.2"
        resolved = resolve_model(model_str)
        llm_id = resolved["llm"]
        base_url = resolved["base_url"]
        api_key = resolved["api_key"]

        # PraisonAI reads OPENAI_BASE_URL and OPENAI_API_KEY from
        # os.environ at Agent construction time.  We snapshot the
        # previous values and restore them immediately after the
        # constructor returns so concurrent threads never see a
        # stale / cross-pollinated endpoint.
        _prev_base = os.environ.get("OPENAI_BASE_URL")
        _prev_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_BASE_URL"] = base_url
        os.environ["OPENAI_API_KEY"] = api_key

        tools = tools_for_sandbox(sandbox)

        agent = Agent(
            name=f"soy-{role.value}-{str(agent_id)[:8]}",
            role=role.value,
            goal=f"Execute tasks assigned to the {role.value} agent.",
            backstory=(
                f"SOY {role.value} agent (id={agent_id}). "
                + (system_prompt or "")
            ),
            instructions=system_prompt or f"You are the {role.value} agent.",
            llm=llm_id,
            base_url=base_url,
            api_key=api_key,
            tools=tools,
        )

        # Restore the previous env so concurrent threads do not see
        # a stale / cross-pollinated endpoint.
        if _prev_base is not None:
            os.environ["OPENAI_BASE_URL"] = _prev_base
        else:
            os.environ.pop("OPENAI_BASE_URL", None)
        if _prev_key is not None:
            os.environ["OPENAI_API_KEY"] = _prev_key
        else:
            os.environ.pop("OPENAI_API_KEY", None)

        return agent

    def _build_praisonai_task(
        self,
        *,
        pa_agent: Any,
        description: str,
        expected_output: Optional[str],
        parallel: bool,
    ) -> Any:
        from praisonaiagents import Task

        return Task(
            description=description,
            expected_output=expected_output or "Task output (JSON or text).",
            agent=pa_agent,
            async_execution=bool(parallel),
        )

    def _build_agents_workflow(
        self,
        *,
        pa_agent: Any,
        pa_task: Any,
        parallel: bool,
    ) -> Any:
        from praisonaiagents import Agents

        return Agents(
            agents=[pa_agent],
            tasks=[pa_task],
            process="parallel" if parallel else "sequential",
        )

    def _run_with_timeout(
        self,
        workflow: Any,
        pa_agent: Any,
        pa_task: Any,
        timeout_seconds: int,
    ) -> tuple[Optional[dict], Optional[str], bool]:
        """Run ``workflow`` under a wall-clock timeout.

        Returns ``(output_dict, error_message, ok)`` where
        ``ok`` is ``True`` only when the workflow completed
        *successfully* (returned a value and did not raise).
        Timeouts, exceptions, and malformed-JSON output all set
        ``ok`` to ``False`` so the caller writes a ``failed``
        execution row and the retry/escalation policy fires.

        The workflow runs on the dedicated ``_workflow_executor``
        (never the fan-out pool) so nested submission from a
        parallel ``execute_mission_tasks`` layer cannot deadlock.

        On timeout the future's cancel token is *set*, but note a
        thread already running ``workflow.start()`` cannot be
        force-interrupted in Python — ``future.cancel()`` is a no-op
        for a started thread. The leaked thread keeps running until
        it returns on its own; its result is discarded and never
        touches the database (this method is the only consumer). True
        interruption of a mid-flight agent (so it cannot keep
        invoking ``run_command``/``file_write`` concurrently with the
        retry) requires per-attempt workspace isolation and
        process-level execution — that lands with the Git-as-SSOT
        execution layer; until then the cancel token is a cooperative
        hint a future tool layer can poll.
        """
        import threading

        cancel_event = threading.Event()
        future = self._workflow_executor.submit(
            self._invoke_workflow, workflow, cancel_event,
        )
        try:
            raw_output = future.result(timeout=timeout_seconds)
        except FutTimeout:
            logger.warning(
                "PraisonAI workflow timed out after %ds; signalling cancel.",
                timeout_seconds,
            )
            cancel_event.set()
            future.cancel()
            return None, "timeout", False
        except Exception as exc:  # noqa: BLE001
            logger.exception("PraisonAI workflow raised: %s", exc)
            return None, f"agent_exception: {exc}", False
        # Parse the output. PraisonAI may return a string, a
        # dict, or a TaskOutput dataclass.
        output = self._coerce_output(raw_output)
        # Malformed JSON output (a value that *looks* like JSON but
        # does not parse) is a failure, not a partial success: the
        # retry policy must fire so a transient bad generation is
        # re-attempted rather than silently recorded as completed.
        if isinstance(output, dict) and output.get("_parse_error"):
            return output, "malformed_json_output", False
        return output, None, True

    @staticmethod
    def _invoke_workflow(workflow: Any, cancel_event: Any = None) -> Any:
        """Call ``.start()`` on the workflow and return the raw output.

        We use ``.start()`` rather than ``.run()`` because
        ``.start()`` returns the final result dict; ``.run()``
        does the same but with verbose console output enabled
        when a TTY is attached. Both return ``None`` when the
        workflow was started without ``return_dict=True`` so we
        explicitly request the dict *when the signature accepts it*.

        ``return_dict`` support is detected once via the call
        signature rather than by catching :class:`TypeError`: a
        blanket ``except TypeError`` would also catch a TypeError
        raised from *inside* a partially-executed workflow (after
        its tools already ran) and re-invoke ``.start()`` a second
        time, doubling external side effects.
        """
        import inspect

        if cancel_event is not None:
            # Expose the token so a (future) cancellation-aware tool
            # layer can poll it; also short-circuit if already set.
            try:
                setattr(workflow, "_soy_cancel_event", cancel_event)
            except Exception:  # noqa: BLE001 — workflow may reject attrs
                pass
            if cancel_event.is_set():
                return None

        start = workflow.start
        try:
            supports_return_dict = "return_dict" in inspect.signature(
                start
            ).parameters or any(
                p.kind == p.VAR_KEYWORD
                for p in inspect.signature(start).parameters.values()
            )
        except (TypeError, ValueError):
            # Builtin / C-implemented callable with no introspectable
            # signature — assume kwargs are accepted and try once.
            supports_return_dict = True

        if supports_return_dict:
            return start(return_dict=True)
        return start()

    @staticmethod
    def _coerce_output(raw: Any) -> Optional[dict]:
        """Convert a raw PraisonAI return value to a JSON-safe dict.

        PraisonAI returns one of:

        * a ``dict`` (when ``return_dict=True`` is supported);
        * a string (Markdown / JSON);
        * a ``TaskOutput`` object with a ``.raw`` / ``.output`` attr;
        * a list of ``TaskOutput`` (for multi-task workflows);
        * ``None``.

        The function returns ``None`` when the raw value cannot
        be turned into a dict, so the caller can flag the
        execution as a partial success (output present but not
        parseable as JSON).
        """
        if raw is None:
            return None
        if isinstance(raw, dict):
            # Recursively serialise; the dict may contain
            # dataclasses / objects that the router then drops
            # when constructing the response.
            try:
                json.dumps(raw, default=str)
                return raw
            except TypeError:
                return {"_raw": repr(raw)}
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return None
            # Try to parse as JSON; if it fails, return the raw
            # string under ``_text`` so the router can surface
            # the value in the API response.
            if stripped.startswith(("{", "[")):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    return {"_text": stripped, "_parse_error": True}
            return {"_text": stripped}
        # Object with attributes (TaskOutput etc.) — try to
        # build a dict.
        obj_dict: Dict[str, Any] = {}
        for attr in ("raw", "output", "result", "description"):
            if hasattr(raw, attr):
                value = getattr(raw, attr)
                if value is not None:
                    obj_dict[attr] = value
        if obj_dict:
            try:
                json.dumps(obj_dict, default=str)
                return obj_dict
            except TypeError:
                return {"_raw": repr(raw)}
        return {"_raw": repr(raw)}

    # ------------------------------------------------------------------
    # Internal: execution recording
    # ------------------------------------------------------------------
    def _record_successful_execution(
        self,
        *,
        task_id: uuid.UUID,
        execution_id: uuid.UUID,
        attempt_number: int,
        started_at: datetime,
        finished_at: datetime,
        output: Optional[dict],
    ) -> TaskExecutionResult:
        """Write the success execution row and mark the task complete."""
        with self._sessionmaker() as db:
            task = db.get(Task, task_id)
            if task is None:
                return TaskExecutionResult(
                    task_id=task_id, status=TaskStatus.failed,
                    error="task_not_found_during_recording",
                )
            execution = Execution(
                id=execution_id,
                task_id=task_id,
                agent_id=task.agent_id,
                mission_id=task.mission_id,
                status=ExecutionStatus.completed,
                attempt_number=attempt_number,
                output=output,
                started_at=started_at,
                finished_at=finished_at,
            )
            db.add(execution)
            task.status = TaskStatus.completed
            # Reset the attempt counter on success — the chain
            # halted early, so the task is done.
            task.attempt_count = attempt_number
            agent = db.get(Agent, task.agent_id)
            if agent is not None:
                agent.status = AgentStatus.completed
            db.commit()
            db.refresh(execution)
            db.refresh(task)
            self._event_publisher(
                "task.completed",
                {
                    "task_id": str(task_id),
                    "mission_id": str(task.mission_id),
                    "execution_id": str(execution.id),
                    "attempt_number": attempt_number,
                },
            )
            return TaskExecutionResult(
                task_id=task_id,
                status=TaskStatus.completed,
                execution_id=execution.id,
                attempt_number=attempt_number,
                output=output,
                attempt_count=attempt_number,
            )

    def _record_failed_execution(
        self,
        *,
        task_id: uuid.UUID,
        execution_id: uuid.UUID,
        attempt_number: int,
        started_at: datetime,
        error: str,
    ) -> TaskExecutionResult:
        """Write a failed execution row and run the retry/escalation rule."""
        finished_at = datetime.now(timezone.utc)
        with self._sessionmaker() as db:
            task = db.get(Task, task_id)
            if task is None:
                return TaskExecutionResult(
                    task_id=task_id, status=TaskStatus.failed,
                    error="task_not_found_during_recording",
                )
            exec_status = (
                ExecutionStatus.timeout
                if error == "timeout"
                else ExecutionStatus.failed
            )
            execution = Execution(
                id=execution_id,
                task_id=task_id,
                agent_id=task.agent_id,
                mission_id=task.mission_id,
                status=exec_status,
                attempt_number=attempt_number,
                error=error,
                started_at=started_at,
                finished_at=finished_at,
            )
            db.add(execution)
            escalated = self._maybe_schedule_retry_or_escalate(
                db, task, attempt_number=attempt_number, error=error,
            )
            db.commit()
            db.refresh(task)
            db.refresh(execution)
            return TaskExecutionResult(
                task_id=task_id,
                status=task.status,
                execution_id=execution.id,
                attempt_number=attempt_number,
                error=error,
                retry_scheduled=(
                    not escalated
                    and task.status == TaskStatus.pending
                    and (task.attempt_count or 0) < MAX_ATTEMPTS
                ),
                escalated=escalated,
                attempt_count=task.attempt_count,
            )

    def _record_execution(
        self,
        db: Session,
        task: Task,
        execution_id: uuid.UUID,
        attempt_number: int,
        started_at: datetime,
        *,
        status: ExecutionStatus,
        error: str,
    ) -> TaskExecutionResult:
        """Write a single execution row inside an open session."""
        execution = Execution(
            id=execution_id,
            task_id=task.id,
            agent_id=task.agent_id,
            mission_id=task.mission_id,
            status=status,
            attempt_number=attempt_number,
            error=error,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(execution)
        escalated = self._maybe_schedule_retry_or_escalate(
            db, task, attempt_number=attempt_number, error=error,
        )
        db.commit()
        db.refresh(task)
        return TaskExecutionResult(
            task_id=task.id,
            status=task.status,
            execution_id=execution.id,
            attempt_number=attempt_number,
            error=error,
            retry_scheduled=(
                not escalated
                and task.status == TaskStatus.pending
                and attempt_number < MAX_ATTEMPTS
            ),
            escalated=escalated,
            attempt_count=task.attempt_count,
        )

    def _maybe_schedule_retry_or_escalate(
        self,
        db: Session,
        task: Task,
        *,
        attempt_number: int,
        error: Optional[str],
    ) -> bool:
        """Apply the 3-try rule.

        * If ``attempt_number < MAX_ATTEMPTS``, the task is reset
          to ``pending`` so the next attempt can re-run.
        * If ``attempt_number == MAX_ATTEMPTS``, the task is
          moved to ``escalated``, the parent mission is moved to
          ``escalated``, and a WebSocket event is emitted.

        Returns ``True`` when the task was escalated.

        The budget keys off the *persisted* ``task.attempt_count``
        (incremented once per attempt in
        :meth:`_execute_single_attempt`) rather than the in-memory
        loop index, so a resumed or re-invoked run honours the global
        3-try budget instead of restarting it.
        """
        if (task.attempt_count or 0) < MAX_ATTEMPTS:
            task.status = TaskStatus.pending
            return False
        task.status = TaskStatus.escalated
        mission = db.get(Mission, task.mission_id)
        if mission is not None and mission.status not in (
            MissionStatus.merged, MissionStatus.escalated,
        ):
            mission.status = MissionStatus.escalated
            mission.updated_at = datetime.now(timezone.utc)
        self._event_publisher(
            "task.escalated",
            {
                "task_id": str(task.id),
                "mission_id": str(task.mission_id),
                "attempt_count": attempt_number,
                "error": error or "max_attempts_reached",
            },
        )
        self._event_publisher(
            "mission.escalated",
            {
                "mission_id": str(task.mission_id),
                "trigger": "task_max_retries",
            },
        )
        return True

    @staticmethod
    def _default_publisher(event: str, payload: Dict[str, Any]) -> None:
        """Best-effort event publisher used when none is injected.

        The SOY worker does not own the WebSocket layer; the
        router wires the actual publisher. Until then we log
        the event so it is visible in PM2 logs.
        """
        logger.info("SOY event %s: %s", event, payload)

    # ------------------------------------------------------------------
    # Coding-agent dispatch path
    # ------------------------------------------------------------------
    def _dispatch_coding_agent(
        self,
        *,
        task_id: uuid.UUID,
        mission_id: uuid.UUID,
        agent_name: str,
        prompt: str,
        execution_id: uuid.UUID,
        attempt_number: int,
        started_at: datetime,
        timeout_seconds: int,
    ) -> TaskExecutionResult:
        """Run the task via a coding-agent CLI (opencode/hermes/codex/droid).

        1. Creates a git worktree for the mission's branch.
        2. Dispatches the coding agent with the task prompt.
        3. Commits + pushes changes, opens a PR.
        4. Records the execution result.
        """
        from soy.services.coding_agent_dispatcher import (
            AgentNotFoundError,
            dispatch as agent_dispatch,
        )
        from soy.services.git_service import GitService

        git = GitService()

        with self._sessionmaker() as db:
            mission = db.get(Mission, mission_id)
            if mission is None:
                return TaskExecutionResult(
                    task_id=task_id, status=TaskStatus.failed,
                    error="mission_not_found",
                )
            md = mission.mission_metadata or {}
            git_info = md.get("git", {})
            branch = mission.branch or git_info.get("branch")

        if not branch:
            return TaskExecutionResult(
                task_id=task_id, status=TaskStatus.failed,
                error="no_git_branch_on_mission",
            )

        # 1. Create worktree
        try:
            worktree_path = git.create_worktree(mission_id, branch)
        except Exception as exc:  # noqa: BLE001
            logger.exception("git worktree failed for mission %s", mission_id)
            return self._record_failed_execution(
                task_id=task_id, execution_id=execution_id,
                attempt_number=attempt_number, started_at=started_at,
                error=f"worktree_failed: {exc}",
            )

        # 2. Dispatch coding agent
        try:
            result = agent_dispatch(
                agent_name, prompt,
                cwd=worktree_path,
                timeout=timeout_seconds,
            )
        except AgentNotFoundError as exc:
            logger.warning("Agent %s not found: %s", agent_name, exc)
            return self._record_failed_execution(
                task_id=task_id, execution_id=execution_id,
                attempt_number=attempt_number, started_at=started_at,
                error=f"agent_not_found: {exc}",
            )

        finished_at = datetime.now(timezone.utc)

        if result.error or result.exit_code != 0:
            return self._record_failed_execution(
                task_id=task_id, execution_id=execution_id,
                attempt_number=attempt_number, started_at=started_at,
                error=result.error or f"agent_exit_{result.exit_code}",
            )

        # 3. Commit + push + open PR
        pr_url = None
        try:
            sha = git.commit_and_push(
                worktree_path,
                f"feat(mission {mission_id}): task {task_id} via {agent_name}",
                branch=branch,
            )
            with self._sessionmaker() as db:
                mission = db.get(Mission, mission_id)
                if mission is not None:
                    md = dict(mission.mission_metadata or {})
                    md.setdefault("git", {})["commit_sha"] = sha
                    md["git"]["agent_exit_code"] = result.exit_code
                    mission.mission_metadata = md
                    db.commit()

            pr_num, pr_url = git.open_pr(
                branch,
                f"Mission {mission_id}: {agent_name} output",
                f"Auto-generated PR from mission {mission_id}\n\n"
                f"Agent: {agent_name}\nModel: {result.model}\n"
                f"Duration: {result.duration_seconds}s",
            )
            with self._sessionmaker() as db:
                mission = db.get(Mission, mission_id)
                if mission is not None:
                    md = dict(mission.mission_metadata or {})
                    md.setdefault("git", {})["pr_number"] = pr_num
                    md["git"]["pr_url"] = pr_url
                    mission.mission_metadata = md
                    db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("git commit/push/PR failed for mission %s", mission_id)
            # Non-fatal: the work was done, just couldn't push

        # 4. Record execution
        output = {
            **result.to_execution_output(),
            "pr_url": pr_url,
            "worktree": worktree_path,
        }
        return self._record_successful_execution(
            task_id=task_id, execution_id=execution_id,
            attempt_number=attempt_number, started_at=started_at,
            finished_at=finished_at, output=output,
        )


def mission_has_git_branch(session_factory, mission_id: uuid.UUID) -> bool:
    """Return True if the mission has a git branch set (Git-as-SSOT active)."""
    try:
        with session_factory() as db:
            mission = db.get(Mission, mission_id)
            if mission is None:
                return False
            md = mission.mission_metadata or {}
            git_info = md.get("git", {})
            return bool(mission.branch or git_info.get("branch"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
# A single worker instance is shared across the FastAPI
# dependency-injection graph. The executor pool is reused across
# requests so timeouts/cancels do not constantly spawn threads.
_worker_singleton: Optional[SoyWorker] = None


def get_worker() -> SoyWorker:
    """Return the singleton :class:`SoyWorker` instance."""
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = SoyWorker()
    return _worker_singleton


def reset_worker() -> None:
    """Drop the cached singleton so the next :func:`get_worker` rebuilds it.

    Tests that change ``SOY_DATABASE_URL`` between cases call
    this so the worker rebuilds its sessionmaker against the
    new URL. The method is a no-op when no worker has been
    constructed yet.

    The executor pool is shut down (without blocking on in-flight
    work) before the singleton is dropped. Otherwise the pool's
    threads outlive the test that spawned them and — because a
    worker resolves ``soy.db.get_session_local()`` — can rebuild the
    cached engine against a *stale* ``SOY_DATABASE_URL`` after the
    next test has already reset it, silently corrupting that test's
    database binding. Production never calls this function (the pool
    is long-lived by design), so the shutdown is a test-isolation
    safeguard only.
    """
    global _worker_singleton
    if _worker_singleton is not None:
        for pool in ("_executor", "_workflow_executor"):
            try:
                getattr(_worker_singleton, pool).shutdown(
                    wait=False, cancel_futures=True,
                )
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
    _worker_singleton = None


def set_event_publisher(
    publisher: Callable[[str, Dict[str, Any]], None],
) -> None:
    """Install a custom event publisher on the singleton worker.

    Called by the WebSocket layer (in a later milestone) so the
    worker broadcasts real-time events. Calling this before the
    worker is constructed is safe — the next :func:`get_worker`
    call picks up the new publisher.
    """
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = SoyWorker(event_publisher=publisher)
    else:
        _worker_singleton._event_publisher = publisher  # type: ignore[attr-defined]


# Re-export for convenience so tests can import everything from
# the worker module.
__all__ = [
    "SoyWorker",
    "DEFAULT_TASK_TIMEOUT_SECONDS",
    "MAX_ATTEMPTS",
    "RETRY_BACKOFF_SECONDS",
    "SANDBOXED_TOOLS",
    "TEAM_ROLE_ORDER",
    "TaskExecutionResult",
    "UNSANDBOXED_TOOLS",
    "get_worker",
    "set_event_publisher",
    "tools_for_sandbox",
]
