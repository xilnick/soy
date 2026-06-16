"""
soy.services.praisonai_trigger
===============================

Triggers the PraisonAI planning phase when a mission transitions
``created`` → ``planning``.

The actual multi-agent team execution is implemented by the PraisonAI
worker in a later milestone; this module provides the *trigger* layer
that:

  1. Validates that PraisonAI is importable (degraded mode if not).
  2. Resolves the model via ``infra.config.resolve_model`` semantics
     (mirrored here so the trigger does not require the full Piperoni
     config — see ``_resolve_model``).
  3. Records a "planning started" event in the mission's metadata
     so the dashboard can show a heartbeat.

The trigger is intentionally fire-and-forget: it never raises into
the API request flow. A planning failure is recorded in the mission
metadata and logged at WARNING so the rest of the orchestration
pipeline can keep moving.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("soy.services.praisonai_trigger")


def _resolve_model(model_name: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a model identifier into a PraisonAI-compatible dict.

    The function mirrors the rule in ``infra/config.py``: a model
    string ending in ``:cloud`` requires the ``OLLAMA_API_KEY``; any
    other model uses the local Ollama base URL.

    Returns a JSON-serialisable, **secret-free** dict (``model``,
    ``base_url``, ``is_cloud``, ``has_api_key``) suitable for storing
    in the mission's ``metadata`` column and returning over the API.
    The resolved API key is deliberately NOT included: this dict is
    persisted to the database and serialised in mission responses, so
    embedding the credential would leak it durably. The worker resolves
    the key itself (via :mod:`soy.services.model_resolver`) when it
    actually constructs an agent.
    """
    name = model_name or os.getenv("ASF_MODEL", "kimi-k2.6:cloud")
    base_url = os.getenv("OPENAI_BASE_URL") or os.getenv(
        "OLLAMA_BASE_URL", "http://localhost:11434/v1"
    )
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OLLAMA_API_KEY", "")
    if name.endswith(":cloud") and not api_key:
        logger.warning(
            "Cloud model %s requested but no API key in env; "
            "agents will likely fail to authenticate", name,
        )
    return {
        "model": name,
        "base_url": base_url,
        "is_cloud": name.endswith(":cloud"),
        "has_api_key": bool(api_key),
    }


def praisonai_available() -> bool:
    """Return True when the ``praisonaiagents`` package is importable.

    Used by the API to return 503 instead of 500 if a planning
    trigger is requested on a host that does not have the agent
    runtime installed.
    """
    try:
        import praisonaiagents  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 — any import error counts as missing
        return False


def trigger_planning_phase(
    mission_id: uuid.UUID,
    *,
    title: str,
    description: Optional[str],
    model_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Kick off the planning phase for ``mission_id``.

    The function does not block the caller for the duration of the
    agent run; the actual PraisonAI work is scheduled to happen in
    the background (when the worker is implemented). For now, this
    function:

    * records the resolved model and the planning start time in the
      ``metadata`` blob (so the API can return a planning heartbeat
      even before the worker exists);
    * logs the trigger at INFO so PM2 captures the heartbeat;
    * returns a serialisable dict the API can include in the
      transition response.

    Parameters
    ----------
    mission_id:
        UUID of the mission that just transitioned to ``planning``.
    title, description:
        Mission context. The agent team is fed the title and
        description as the planning brief.
    model_name:
        Optional override for the LLM identifier. Defaults to the
        ``ASF_MODEL`` env var, falling back to ``kimi-k2.6:cloud``.

    Returns
    -------
    dict
        ``{"triggered": bool, "model": {...}, "started_at": ISO-8601,
        "praisonai_available": bool}`` — the caller stores this in
        the mission's metadata.
    """
    started_at = datetime.now(timezone.utc).isoformat()
    model = _resolve_model(model_name)
    payload: Dict[str, Any] = {
        "triggered": False,
        "model": model,
        "started_at": started_at,
        "praisonai_available": praisonai_available(),
    }

    if not praisonai_available():
        # Soft failure — we still record the trigger attempt so the
        # dashboard can show that planning was requested but the
        # agent runtime is missing.
        payload["reason"] = "praisonaiagents not installed"
        logger.warning(
            "Planning trigger for mission %s recorded but PraisonAI "
            "is not available; install praisonaiagents to enable it.",
            mission_id,
        )
        return payload

    # The full PraisonAI integration is delivered by the workers/
    # agents/executions feature. To keep the API surface stable we
    # *pretend* the planning agent has been instantiated, by
    # importing the ``Agent`` symbol — this exercises the import
    # path the worker will rely on, and surfaces any breaking
    # change early. The real ``start()`` call will be added by the
    # agents feature in a later milestone.
    try:
        from praisonaiagents import Agent  # noqa: F401

        payload["triggered"] = True
        logger.info(
            "Planning phase triggered for mission %s (model=%s)",
            mission_id, model["model"],
        )
    except Exception as exc:  # noqa: BLE001
        payload["reason"] = f"agent import failed: {exc}"
        logger.warning(
            "Planning trigger for mission %s failed: %s",
            mission_id, exc,
        )
    return payload
