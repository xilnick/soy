"""
soy.config
==========

Runtime configuration for the Soy backend's optional integrations
(structured logging, Git-as-SSOT, Mission Control sync, DeerFlow).

Every value is read from the environment at *call time* — never cached
at import — for the same reason :mod:`soy.db` reads ``ASF_DATABASE_URL``
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
    return os.getenv("ASF_LOG_FORMAT", "json").strip().lower()


def log_level() -> str:
    """Root log level name (default ``INFO``)."""
    return os.getenv("ASF_LOG_LEVEL", "INFO").strip().upper()


# ---------------------------------------------------------------------------
# Mission Control data sync (ASF → MC REST)
# ---------------------------------------------------------------------------
def mc_sync_enabled() -> bool:
    return _bool("ASF_MC_SYNC_ENABLED", False)


def mc_base_url() -> str:
    return os.getenv("ASF_MC_BASE_URL", "http://127.0.0.1:3003").rstrip("/")


def mc_api_key() -> str:
    return os.getenv("MC_API_KEY", "")


def mc_timeout_seconds() -> float:
    # Tight by default: a slow/down MC must never degrade ASF latency.
    return float(os.getenv("ASF_MC_TIMEOUT_SECONDS", "2"))


# ---------------------------------------------------------------------------
# DeerFlow sandbox integration
# ---------------------------------------------------------------------------
def deerflow_enabled() -> bool:
    return _bool("ASF_DEERFLOW_ENABLED", False)


def deerflow_base_url() -> str:
    return os.getenv("ASF_DEERFLOW_BASE_URL", "http://127.0.0.1:2026").rstrip("/")


def deerflow_timeout_seconds() -> float:
    return float(os.getenv("ASF_DEERFLOW_TIMEOUT_SECONDS", "5"))


# ---------------------------------------------------------------------------
# Git-as-SSOT (GitHub webhook + branch/spec commit)
# ---------------------------------------------------------------------------
def github_webhook_secret() -> str:
    """Shared secret for ``X-Hub-Signature-256`` validation.

    Empty means the webhook is default-denied (no secret configured →
    no request is trusted).
    """
    return os.getenv("ASF_GITHUB_WEBHOOK_SECRET", "")


def asf_run_label() -> str:
    """Issue label that triggers a mission (default ``asf-run``)."""
    return os.getenv("ASF_RUN_LABEL", "asf-run").strip()


def git_enabled() -> bool:
    """When True, the webhook creates the feature branch + spec.md."""
    return _bool("ASF_GIT_ENABLED", False)


def git_workdir() -> str:
    """Base directory for per-mission working clones."""
    return os.getenv("ASF_GIT_WORKDIR", os.path.expanduser("~/asf/work"))


def git_author_name() -> str:
    return os.getenv("ASF_GIT_AUTHOR_NAME", "ASF Bot")


def git_author_email() -> str:
    return os.getenv("ASF_GIT_AUTHOR_EMAIL", "asf-bot@piperoni.local")


def git_push_enabled() -> bool:
    """When True, the spec commit is pushed to ``origin``."""
    return _bool("ASF_GIT_PUSH_ENABLED", False)


def git_spec_path() -> str:
    """Repo-relative path of the spec file written on the branch."""
    return os.getenv("ASF_GIT_SPEC_PATH", "spec.md")
