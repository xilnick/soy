"""
soy.services.model_resolver
============================

Resolves a model identifier (e.g. ``"kimi-k2.6:cloud"`` or
``"ollama/codestral"``) into the LLM configuration PraisonAI needs
to instantiate an ``Agent``.

The function delegates to ``infra.config.resolve_model`` when the
package is importable (so the resolution rule lives in one place
across the Piperoni stack); it falls back to a local copy otherwise
so the ASF worker can still spin up agents on a host where the
full Piperoni config is not on ``sys.path``.

Routing rules
-------------

* Models starting with ``ollama/`` route to the local Ollama
  instance (``OLLAMA_BASE_URL``) with no API key (PrairieAI's
  default).
* Models ending in ``:cloud`` route to the cloud endpoint with the
  ``OLLAMA_API_KEY`` (Ollama Cloud uses the same auth header as
  the OpenAI-compatible API).
* Any other identifier is treated as an Ollama-native model name
  and routed to the local Ollama base URL.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("soy.services.model_resolver")

# Default Ollama base URL (the ``/v1`` suffix matches the
# OpenAI-compatible endpoint that PraisonAI calls).
_DEFAULT_OLLAMA_BASE = "http://localhost:11434/v1"


def _ollama_base_url() -> str:
    """Return the Ollama base URL from env, with safe fallback."""
    return os.getenv("OPENAI_BASE_URL") or os.getenv(
        "OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE
    )


def _ollama_api_key() -> str:
    """Return the Ollama API key from env, with safe fallback."""
    return os.getenv("OPENAI_API_KEY") or os.getenv("OLLAMA_API_KEY", "")


def resolve_model(model_str: str) -> Dict[str, Any]:
    """Resolve ``model_str`` into a PraisonAI-compatible config dict.

    The returned dict is JSON-serialisable and contains:

    * ``model``    — the model identifier PraisonAI sees.
    * ``base_url`` — the OpenAI-compatible endpoint.
    * ``api_key``  — the bearer token (or ``"ollama"`` for local).
    * ``llm``      — the value to pass to ``Agent(llm=...)``: for
                      cloud models it is the same ``model`` string,
                      for local Ollama models it is the model id
                      after stripping the ``ollama/`` prefix.
    * ``is_cloud`` — boolean; ``True`` for ``:cloud`` models.

    Routing rules (in order):

    1. If ``model_str`` starts with ``"ollama/"`` (e.g.
       ``"ollama/codestral"``) → local Ollama at
       ``OPENAI_BASE_URL``/``OLLAMA_BASE_URL``, no key required.
    2. If ``model_str`` ends with ``":cloud"`` → cloud endpoint
       (still OLLAMA_BASE_URL — Ollama Cloud is served from the
       same hostname) with ``OLLAMA_API_KEY`` injected.
    3. Otherwise treat the identifier as an Ollama-native name and
       route to the local Ollama endpoint.
    """
    if not model_str or not model_str.strip():
        raise ValueError("model_str must be a non-empty string")

    raw = model_str.strip()

    # The ASF resolver implements the routing rules locally so
    # the worker can operate on hosts that do not have the
    # ``infra`` package on ``sys.path`` (e.g. lightweight
    # ASF-only unit tests). The rules match what the
    # ``infra.config.resolve_model`` helper does, with two
    # additions that the worker relies on:

    # * the ``ollama/`` prefix is stripped from the LLM id so
    #   PraisonAI receives the bare model name;
    # * the ``OPENAI_BASE_URL`` env var takes precedence over
    #   ``OLLAMA_BASE_URL`` so test rigs can re-point the worker
    #   at a mock endpoint.

    base_url = _ollama_base_url()
    api_key = _ollama_api_key()
    is_cloud = False
    llm_id = raw

    if raw.lower().startswith("ollama/"):
        # ``ollama/<model>`` — explicit local routing.
        llm_id = raw.split("/", 1)[1]
        # Local Ollama accepts any non-empty bearer token. Fill
        # in the conventional ``"ollama"`` placeholder when no
        # key is configured so PraisonAI's OpenAI client does
        # not fail with an empty header.
        if not api_key:
            api_key = "ollama"
    elif raw.endswith(":cloud"):
        # Cloud model — needs the API key. We do NOT fall
        # back to the ``"ollama"`` placeholder here because
        # the cloud endpoint *does* validate the key; filling
        # in a placeholder would silently mis-route the
        # request to a local endpoint.
        is_cloud = True
        llm_id = raw[: -len(":cloud")]
    else:
        # Bare name treated as local Ollama model — same
        # placeholder rule as the ``ollama/`` branch.
        if not api_key:
            api_key = "ollama"
    # else: bare name, treated as local Ollama model.

    if is_cloud and not api_key:
        logger.warning(
            "Cloud model %s requested but no OLLAMA_API_KEY in env; "
            "agent instantiation will fail until the key is set.",
            raw,
        )

    return {
        "model": raw,
        "llm": llm_id,
        "base_url": base_url,
        "api_key": api_key,
        "is_cloud": is_cloud,
        "provider": "ollama",
    }


def export_env_for_praisonai(model_str: Optional[str] = None) -> Dict[str, str]:
    """Compute the env vars the PraisonAI worker should export.

    PraisonAI reads ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` from
    the environment at agent-instantiation time, so the worker
    must set them before importing ``praisonaiagents`` (the values
    are baked into the agent's LLM client at construction).

    The dict is meant to be merged into ``os.environ`` *after* the
    process has started, so it does not mutate global state itself.
    """
    if model_str is None:
        return {
            "OPENAI_BASE_URL": _ollama_base_url(),
            "OPENAI_API_KEY": _ollama_api_key(),
        }
    resolved = resolve_model(model_str)
    return {
        "OPENAI_BASE_URL": resolved["base_url"],
        "OPENAI_API_KEY": resolved["api_key"],
    }


def praisonai_agent_model_id(model_str: str) -> str:
    """Return the LLM identifier to feed ``Agent(llm=...)``.

    The conversion strips the ``ollama/`` prefix (PraisonAI
    already speaks the OpenAI API and the prefix is only useful
    for routing decisions in the application layer) and the
    ``:cloud`` suffix (the model id is the same on both
    endpoints).
    """
    resolved = resolve_model(model_str)
    return resolved["llm"]


# Pattern used by unit tests to assert the worker respects the
# routing contract.
_OLLAMA_PREFIX_RE = re.compile(r"^ollama/(?P<model>.+)$", re.IGNORECASE)


def is_ollama_local(model_str: str) -> bool:
    """Return True when the model routes to the local Ollama instance."""
    if not model_str:
        return False
    raw = model_str.strip()
    if raw.lower().startswith("ollama/"):
        return True
    return not raw.endswith(":cloud")
