"""
soy.services.coding_agent_dispatcher
====================================

Reads coding-agent manifests from ``config/agents/*.json`` and invokes the
matching CLI binary as a subprocess. Returns a structured result that
the PraisonAI worker stores on the ``Execution`` row.

Gated by ``SOY_CODING_AGENT_ENABLED`` (default True). Never raises from
``dispatch()`` — failures are captured as ``DispatchResult.error``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from soy import config

logger = logging.getLogger("soy.services.coding_agent_dispatcher")


class AgentNotFoundError(KeyError):
    """No manifest exists for the requested agent name."""


class AgentTimeoutError(TimeoutError):
    """The agent subprocess exceeded its timeout."""


@dataclass
class AgentManifest:
    name: str
    binary: str
    default_model: str
    invocation: str
    env: Dict[str, str]
    healthcheck: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> AgentManifest:
        return cls(
            name=d["name"],
            binary=d["binary"],
            default_model=d.get("default_model", ""),
            invocation=d["invocation"],
            env=d.get("env", {}),
            healthcheck=d.get("healthcheck", ""),
        )


@dataclass
class DispatchResult:
    stdout: str
    stderr: str
    exit_code: int
    binary: str
    model: str
    duration_seconds: float
    error: Optional[str] = None

    def to_execution_output(self) -> Dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "binary": self.binary,
            "model": self.model,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
        }


def load_agents(config_dir: str | Path | None = None) -> Dict[str, AgentManifest]:
    """Scan *config_dir* for ``*.json`` manifests and return ``{name: manifest}``.

    Silently skips files with invalid JSON (logs a warning).
    Raises ``FileNotFoundError`` if the directory itself is missing.
    """
    config_dir = Path(config_dir or config.agent_manifest_dir())
    if not config_dir.is_dir():
        raise FileNotFoundError(f"Agent manifest directory not found: {config_dir}")

    agents: Dict[str, AgentManifest] = {}
    for path in sorted(config_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text())
            manifest = AgentManifest.from_dict(raw)
            agents[manifest.name] = manifest
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Skipping invalid agent manifest %s: %s", path, exc)
    return agents


def _build_command(manifest: AgentManifest, prompt: str, model: str) -> list[str]:
    """Substitute placeholders in the invocation template and return the
    command as a list of shell tokens."""
    invocation = manifest.invocation
    invocation = invocation.replace("{binary}", manifest.binary)
    invocation = invocation.replace("{model}", model)
    invocation = invocation.replace("{prompt}", prompt)
    return invocation.split()


def dispatch(
    agent_name: str,
    prompt: str,
    *,
    cwd: str | None = None,
    model: str | None = None,
    timeout: int | None = None,
    config_dir: str | Path | None = None,
) -> DispatchResult:
    """Invoke a coding agent and return a structured result.

    Parameters
    ----------
    agent_name:
        Name matching a manifest file in *config_dir*.
    prompt:
        The prompt/instruction to pass to the agent.
    cwd:
        Working directory for the subprocess (e.g. a git worktree).
    model:
        Override the manifest's ``default_model``. Falls back to
        ``manifest.default_model`` if not provided.
    timeout:
        Wall-clock timeout in seconds. Defaults to
        ``config.agent_timeout_seconds()``.
    config_dir:
        Path to the manifest directory. Defaults to
        ``config.agent_manifest_dir()``.

    Raises
    ------
    AgentNotFoundError
        If no manifest exists for *agent_name*.
    AgentTimeoutError
        If the subprocess exceeds *timeout*.
    """
    agents = load_agents(config_dir)
    if agent_name not in agents:
        raise AgentNotFoundError(f"No agent manifest for '{agent_name}'")

    manifest = agents[agent_name]
    resolved_model = model or manifest.default_model

    timeout = timeout or config.agent_timeout_seconds()

    cmd = _build_command(manifest, prompt, resolved_model)

    env = dict(__import__("os").environ)
    env.update(manifest.env)

    logger.info(
        "Dispatching agent=%s binary=%s model=%s cwd=%s timeout=%ds",
        agent_name, manifest.binary, resolved_model, cwd, timeout,
    )

    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.monotonic() - start
        return DispatchResult(
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            binary=manifest.binary,
            model=resolved_model,
            duration_seconds=round(elapsed, 2),
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return DispatchResult(
            stdout=stdout,
            stderr=stderr + f"\nAgent timed out after {elapsed:.0f}s",
            exit_code=-1,
            binary=manifest.binary,
            model=resolved_model,
            duration_seconds=round(elapsed, 2),
            error=f"timeout after {elapsed:.0f}s",
        )
    except FileNotFoundError:
        elapsed = time.monotonic() - start
        return DispatchResult(
            stdout="",
            stderr=f"Agent binary not found: {manifest.binary}",
            exit_code=-1,
            binary=manifest.binary,
            model=resolved_model,
            duration_seconds=round(elapsed, 2),
            error=f"binary not found: {manifest.binary}",
        )
