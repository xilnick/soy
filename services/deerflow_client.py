"""
asf.services.deerflow_client
============================

Optional integration with the DeerFlow sandbox (the shared Piperoni
service on port 2026). The ASF backend can trigger a sandboxed task
run in DeerFlow instead of executing tools directly — the safe path for
agents whose ``sandbox`` flag is set.

Gated off by default (``ASF_DEERFLOW_ENABLED``); all calls are
best-effort with a timeout and never raise into the caller.

.. note::

   DeerFlow's exact REST contract is **assumed** here (a
   ``POST /api/sandbox/tasks`` trigger + a ``GET /api/health`` probe)
   and is NOT verified against a running DeerFlow instance. Adjust the
   paths/payload below once the contract is confirmed. The
   :func:`maybe_trigger_sandbox` helper is the integration seam the
   execution layer calls when sandboxed execution is wired (it ties to
   the agent ``sandbox`` flag — see
   :func:`asf.services.praisonai_worker.tools_for_sandbox`).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from asf import config

logger = logging.getLogger("asf.services.deerflow_client")


class DeerFlowClient:
    """Best-effort REST client for the DeerFlow sandbox."""

    def __init__(
        self, *, base_url: str | None = None, timeout: float | None = None,
    ) -> None:
        self.base_url = base_url or config.deerflow_base_url()
        self.timeout = timeout or config.deerflow_timeout_seconds()

    def health(self) -> bool:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{self.base_url}/api/health")
                resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("DeerFlow health check failed: %s", exc)
            return False

    def trigger_sandbox_task(
        self,
        *,
        task_id: str,
        description: str,
        tools: Optional[list] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Trigger a sandboxed run in DeerFlow; return its response JSON.

        Returns ``None`` on any failure (logged) so the caller can fall
        back to direct execution. Never raises.
        """
        import httpx

        payload = {
            "task_id": task_id,
            "description": description,
            "tools": tools or [],
            "metadata": metadata or {},
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/api/sandbox/tasks", json=payload,
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "DeerFlow sandbox trigger failed for task %s: %s",
                task_id, exc,
            )
            return None


def maybe_trigger_sandbox(
    *,
    task_id: str,
    description: str,
    sandbox: bool,
    tools: Optional[list] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Trigger a DeerFlow sandbox run iff enabled AND the agent is sandboxed.

    Gated by ``ASF_DEERFLOW_ENABLED``; a genuine no-op (returns ``None``
    without touching the network) when the feature is off or the agent
    is not sandboxed. This is the seam the execution layer calls.
    """
    if not config.deerflow_enabled() or not sandbox:
        return None
    return DeerFlowClient().trigger_sandbox_task(
        task_id=task_id, description=description, tools=tools, metadata=metadata,
    )
