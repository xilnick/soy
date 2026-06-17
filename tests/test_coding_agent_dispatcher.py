"""Tests for soy.services.coding_agent_dispatcher."""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixture: temporary manifest directory
# ---------------------------------------------------------------------------

@pytest.fixture
def manifest_dir(tmp_path):
    """Create a temp directory with two valid manifests and one invalid."""
    opencode = {
        "name": "opencode",
        "binary": "/usr/local/bin/opencode",
        "default_model": "kimi-k2.6",
        "invocation": "{binary} exec --model {model} --prompt -",
        "env": {"OPENAI_BASE_URL": "https://openrouter.ai/api/v1"},
        "healthcheck": "command -v opencode &>/dev/null",
    }
    droid = {
        "name": "droid",
        "binary": "/usr/local/bin/droid",
        "default_model": "default",
        "invocation": "{binary} exec --prompt {prompt}",
        "env": {},
        "healthcheck": "command -v droid &>/dev/null",
    }
    (tmp_path / "opencode.json").write_text(json.dumps(opencode))
    (tmp_path / "droid.json").write_text(json.dumps(droid))
    (tmp_path / "bad.json").write_text("not valid json{{{")
    return tmp_path


# ---------------------------------------------------------------------------
# load_agents
# ---------------------------------------------------------------------------

class TestLoadAgents:
    def test_loads_valid_manifests(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import load_agents
        agents = load_agents(manifest_dir)
        assert set(agents.keys()) == {"droid", "opencode"}

    def test_skips_invalid_json(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import load_agents
        agents = load_agents(manifest_dir)
        assert "bad" not in agents

    def test_missing_dir_raises(self, tmp_path):
        from soy.services.coding_agent_dispatcher import load_agents
        with pytest.raises(FileNotFoundError):
            load_agents(tmp_path / "nonexistent")

    def test_manifest_fields(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import load_agents
        agents = load_agents(manifest_dir)
        oc = agents["opencode"]
        assert oc.binary == "/usr/local/bin/opencode"
        assert oc.default_model == "kimi-k2.6"
        assert oc.env == {"OPENAI_BASE_URL": "https://openrouter.ai/api/v1"}


# ---------------------------------------------------------------------------
# dispatch — with mocked subprocess
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_dispatch_success(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch

        fake_result = mock.Mock()
        fake_result.stdout = "hello output"
        fake_result.stderr = ""
        fake_result.returncode = 0

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            result = dispatch(
                "opencode", "do something",
                config_dir=manifest_dir,
                timeout=30,
            )

        assert result.exit_code == 0
        assert result.stdout == "hello output"
        assert result.binary == "/usr/local/bin/opencode"
        assert result.model == "kimi-k2.6"
        assert result.duration_seconds >= 0

        # Verify the command was built correctly
        cmd = m_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/opencode"
        assert "--model" in cmd
        assert "kimi-k2.6" in cmd

    def test_dispatch_custom_model(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch

        fake_result = mock.Mock()
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            dispatch(
                "opencode", "do something",
                config_dir=manifest_dir,
                model="gpt-4o",
            )

        cmd = m_run.call_args[0][0]
        assert "gpt-4o" in cmd

    def test_dispatch_droid_uses_prompt(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch

        fake_result = mock.Mock()
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            dispatch(
                "droid", "fix the tests",
                config_dir=manifest_dir,
            )

        cmd = m_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/droid"
        # droid invocation substitutes {prompt} inline, not via stdin
        assert "fix" in " ".join(cmd)

    def test_dispatch_missing_agent(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch, AgentNotFoundError
        with pytest.raises(AgentNotFoundError):
            dispatch("nonexistent", "prompt", config_dir=manifest_dir)

    def test_dispatch_timeout_returns_result(self, manifest_dir):
        import subprocess as _sp
        from soy.services.coding_agent_dispatcher import dispatch

        def _timeout(*args, **kwargs):
            raise _sp.TimeoutExpired(cmd="test", timeout=10, output=b"partial", stderr=b"")

        with mock.patch("subprocess.run", side_effect=_timeout):
            result = dispatch(
                "opencode", "do something",
                config_dir=manifest_dir,
                timeout=5,
            )

        assert result.exit_code == -1
        assert result.error is not None
        assert "timeout" in result.error

    def test_dispatch_binary_not_found(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch

        def _not_found(*args, **kwargs):
            raise FileNotFoundError("No such file")

        with mock.patch("subprocess.run", side_effect=_not_found):
            result = dispatch(
                "opencode", "do something",
                config_dir=manifest_dir,
                timeout=5,
            )

        assert result.exit_code == -1
        assert "not found" in result.error

    def test_dispatch_env_merged(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch

        fake_result = mock.Mock()
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            with mock.patch.dict(os.environ, {"MY_VAR": "test_val"}, clear=False):
                dispatch("opencode", "prompt", config_dir=manifest_dir)

        env_used = m_run.call_args[1]["env"]
        assert env_used["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"
        assert env_used["MY_VAR"] == "test_val"

    def test_dispatch_cwd_passed(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import dispatch

        fake_result = mock.Mock()
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with mock.patch("subprocess.run", return_value=fake_result) as m_run:
            dispatch("opencode", "prompt", cwd="/tmp/worktree", config_dir=manifest_dir)

        assert m_run.call_args[1]["cwd"] == "/tmp/worktree"


# ---------------------------------------------------------------------------
# DispatchResult
# ---------------------------------------------------------------------------

class TestDispatchResult:
    def test_to_execution_output(self, manifest_dir):
        from soy.services.coding_agent_dispatcher import (
            DispatchResult,
        )
        r = DispatchResult(
            stdout="out", stderr="err", exit_code=0,
            binary="/bin/x", model="m", duration_seconds=1.5,
        )
        d = r.to_execution_output()
        assert d["stdout"] == "out"
        assert d["exit_code"] == 0
        assert d["duration_seconds"] == 1.5
        assert d["error"] is None
