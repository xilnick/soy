"""
soy.services.mission_control_sync
=================================

Pushes a read-replica of ASF state (agents, tasks, mission status) to
Mission Control's REST API so the dashboard can render it.

Design constraints:

* **Gated off by default** (``ASF_MC_SYNC_ENABLED``). When disabled the
  module-level ``sync_*`` helpers are a genuine no-op — they never
  construct a client or touch the network — so the core API has no MC
  dependency unless an operator opts in.
* **Never blocks or breaks the request.** Every POST uses a tight
  timeout (``ASF_MC_TIMEOUT_SECONDS``, default 2s) and swallows all
  errors (logged at WARNING). A slow or down MC degrades nothing.

.. note::

   The exact Mission Control request schema is **assumed** from the
   integration plan (the MC endpoints ``/api/agents``, ``/api/tasks``,
   ``/api/status``) and the ASF entity shapes — it is NOT verified
   against a running MC instance here. The payload builders below are
   the single place to adjust once MC's contract is confirmed.

   For a high-throughput deployment the synchronous-best-effort POSTs
   should move to a background queue; gated + tight-timeout is the M2
   minimum that keeps request latency bounded.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from soy import config

logger = logging.getLogger("soy.services.mission_control_sync")


def _enum(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def agent_payload(agent: Any) -> Dict[str, Any]:
    return {
        "id": str(agent.id),
        "mission_id": str(agent.mission_id),
        "name": agent.name,
        "role": _enum(agent.role),
        "status": _enum(agent.status),
        "model": agent.model,
    }


def task_payload(task: Any) -> Dict[str, Any]:
    return {
        "id": str(task.id),
        "mission_id": str(task.mission_id),
        "agent_id": str(task.agent_id) if task.agent_id else None,
        "description": task.description,
        "status": _enum(task.status),
        "attempt_count": task.attempt_count,
    }


def mission_status_payload(mission: Any) -> Dict[str, Any]:
    return {
        "id": str(mission.id),
        "title": mission.title,
        "status": _enum(mission.status),
        "branch": mission.branch,
        "repo_url": mission.repo_url,
    }


class MissionControlSync:
    """Thin best-effort REST client for Mission Control."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = base_url or config.mc_base_url()
        self.api_key = config.mc_api_key() if api_key is None else api_key
        self.timeout = timeout or config.mc_timeout_seconds()

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # Send both common shapes; MC accepts one of them.
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    def _post(self, path: str, payload: Dict[str, Any]) -> bool:
        """POST ``payload`` to ``path``; return success. Never raises."""
        import httpx

        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=self._headers())
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort sync
            logger.warning("MC sync POST %s failed: %s", path, exc)
            return False

    def push_agent(self, agent: Any) -> bool:
        return self._post("/api/agents", agent_payload(agent))

    def push_task(self, task: Any) -> bool:
        return self._post("/api/tasks", task_payload(task))

    def push_mission_status(self, mission: Any) -> bool:
        return self._post("/api/status", mission_status_payload(mission))


# ---------------------------------------------------------------------------
# Gated, fire-safe module helpers (what the routers call)
# ---------------------------------------------------------------------------
def sync_agent(agent: Any) -> None:
    if not config.mc_sync_enabled():
        return
    try:
        MissionControlSync().push_agent(agent)
    except Exception:  # noqa: BLE001 — belt-and-suspenders
        logger.exception("MC sync_agent failed")


def sync_task(task: Any) -> None:
    if not config.mc_sync_enabled():
        return
    try:
        MissionControlSync().push_task(task)
    except Exception:  # noqa: BLE001
        logger.exception("MC sync_task failed")


def sync_mission_status(mission: Any) -> None:
    if not config.mc_sync_enabled():
        return
    try:
        MissionControlSync().push_mission_status(mission)
    except Exception:  # noqa: BLE001
        logger.exception("MC sync_mission_status failed")
