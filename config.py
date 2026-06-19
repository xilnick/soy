"""
soy.config
==========

Runtime configuration for the Soy backend's optional integrations
(structured logging, Git-as-SSOT, Mission Control sync, DeerFlow).

Every value is read from the environment at *call time* — never cached
at import — for the same reason :mod:`soy.db` reads ``SOY_DATABASE_URL``
lazily: the Piperoni deploy writes ``~/repos/soy/.env`` before the process
starts, and unit tests ``monkeypatch.setenv`` between cases. A cached
import-time read would freeze the value and silently defeat both.

Design note — *gated off by default*: every external integration
(MC sync, DeerFlow, Git push, the Git-SSOT step, the GitHub webhook)
defaults to disabled / no-op. The features are opt-in via env flags so
the core API has zero new outbound dependencies unless explicitly
enabled by the operator. Tests enable a flag and assert the behaviour,
and separately assert that the flag-off path is a genuine no-op.
"""

from __future__ import annotations

import os


def _bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var (1/true/yes/on, case-insensitive)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log_format() -> str:
    """``"json"`` (default) or ``"text"`` — the stdout log line format."""
    return os.getenv("SOY_LOG_FORMAT", "json").strip().lower()


def log_level() -> str:
    """Root log level name (default ``INFO``)."""
    return os.getenv("SOY_LOG_LEVEL", "INFO").strip().upper()


# ---------------------------------------------------------------------------
# Mission Control data sync (SOY → MC REST)
# ---------------------------------------------------------------------------
def mc_sync_enabled() -> bool:
    return _bool("SOY_MC_SYNC_ENABLED", False)


def mc_base_url() -> str:
    return os.getenv("SOY_MC_BASE_URL", "http://127.0.0.1:3003").rstrip("/")


def mc_api_key() -> str:
    return os.getenv("MC_API_KEY", "")


def mc_timeout_seconds() -> float:
    # Tight by default: a slow/down MC must never degrade SOY latency.
    return float(os.getenv("SOY_MC_TIMEOUT_SECONDS", "2"))


# ---------------------------------------------------------------------------
# DeerFlow sandbox integration
# ---------------------------------------------------------------------------
def deerflow_enabled() -> bool:
    return _bool("SOY_DEERFLOW_ENABLED", False)


def deerflow_base_url() -> str:
    return os.getenv("SOY_DEERFLOW_BASE_URL", "http://127.0.0.1:2026").rstrip("/")


def deerflow_timeout_seconds() -> float:
    return float(os.getenv("SOY_DEERFLOW_TIMEOUT_SECONDS", "5"))


# ---------------------------------------------------------------------------
# Git-as-SSOT (GitHub webhook + branch/spec commit)
# ---------------------------------------------------------------------------
def github_webhook_secret() -> str:
    """Shared secret for ``X-Hub-Signature-256`` validation.

    Empty means the webhook is default-denied (no secret configured →
    no request is trusted).
    """
    return os.getenv("SOY_GITHUB_WEBHOOK_SECRET", "")


def soy_run_label() -> str:
    """Issue label that triggers a mission (default ``soy-run``)."""
    return os.getenv("SOY_RUN_LABEL", "soy-run").strip()


def git_enabled() -> bool:
    """When True, the webhook creates the feature branch + spec.md."""
    return _bool("SOY_GIT_ENABLED", False)


def git_workdir() -> str:
    """Base directory for per-mission working clones."""
    return os.getenv("SOY_GIT_WORKDIR", os.path.expanduser("~/soy/work"))


def git_author_name() -> str:
    return os.getenv("SOY_GIT_AUTHOR_NAME", "Soy Bot")


def git_author_email() -> str:
    return os.getenv("SOY_GIT_AUTHOR_EMAIL", "soy-bot@piperoni.local")


def git_push_enabled() -> bool:
    """When True, the spec commit is pushed to ``origin``."""
    return _bool("SOY_GIT_PUSH_ENABLED", False)


def git_spec_path() -> str:
    """Repo-relative path of the spec file written on the branch."""
    return os.getenv("SOY_GIT_SPEC_PATH", "spec.md")


# ---------------------------------------------------------------------------
# Coding agent dispatch
# ---------------------------------------------------------------------------
def coding_agent_enabled() -> bool:
    """When True, the mission execution step can invoke coding agent CLIs."""
    return _bool("SOY_CODING_AGENT_ENABLED", True)


def agent_timeout_seconds() -> int:
    """Wall-clock timeout for a single coding-agent subprocess call."""
    return int(os.getenv("SOY_AGENT_TIMEOUT_SECONDS", "600"))


def agent_manifest_dir() -> str:
    """Path to the directory containing coding-agent JSON manifests."""
    return os.getenv(
        "SOY_AGENT_MANIFEST_DIR",
        os.path.expanduser("~/repos/soy/config/agents"),
    )


# ---------------------------------------------------------------------------
# Agent routing: which agent handles which mission phase
# ---------------------------------------------------------------------------
def research_agent() -> str:
    """Agent name for the research phase (default: hermes)."""
    return os.getenv("SOY_RESEARCH_AGENT", "hermes")


def implementation_agent() -> str:
    """Agent name for the implementation phase (default: droid)."""
    return os.getenv("SOY_IMPLEMENTATION_AGENT", "droid")


# ---------------------------------------------------------------------------
# Optional plan-review model (GLM 5.2 or similar)
# ---------------------------------------------------------------------------
def review_model() -> str:
    """Model identifier for the plan-review step. Empty = disabled."""
    return os.getenv("SOY_REVIEW_MODEL", "")


def review_enabled() -> bool:
    """True when SOY_REVIEW_MODEL is set and non-empty."""
    return bool(review_model().strip())
