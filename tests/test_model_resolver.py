"""
Tests for :mod:`soy.services.model_resolver`.

Covers the routing contract that the ASF worker relies on:

* Ollama-prefixed models route to the local Ollama base URL
  with no API key.
* ``:cloud`` models inject the ``OLLAMA_API_KEY`` (Ollama Cloud
  uses the same auth header as the OpenAI-compatible API).
* Bare names default to local Ollama.
* The function never raises on a missing key — it logs a
  warning and returns the empty key so the caller can decide
  what to do.
"""

from __future__ import annotations

import os

import pytest


def test_resolve_ollama_prefix_routes_to_local(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from soy.services.model_resolver import resolve_model

    out = resolve_model("ollama/codestral")
    assert out["base_url"] == "http://localhost:11434/v1"
    # The local Ollama endpoint accepts a non-empty key — the
    # resolver fills in ``"ollama"`` so callers don't have to
    # special-case empty keys.
    assert out["api_key"] == "ollama"
    assert out["is_cloud"] is False
    assert out["llm"] == "codestral"


def test_resolve_cloud_model_injects_api_key(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test-cloud-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from soy.services.model_resolver import resolve_model

    out = resolve_model("kimi-k2.6:cloud")
    assert out["base_url"] == "http://localhost:11434/v1"
    assert out["api_key"] == "sk-test-cloud-key"
    assert out["is_cloud"] is True
    # The :cloud suffix is stripped from the llm identifier.
    assert out["llm"] == "kimi-k2.6"


def test_resolve_bare_name_defaults_to_local(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from soy.services.model_resolver import resolve_model

    out = resolve_model("llama3.2")
    assert out["is_cloud"] is False
    assert out["base_url"] == "http://localhost:11434/v1"
    assert out["llm"] == "llama3.2"


def test_resolve_uses_openai_base_url_override(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://cloud-ollama.example/v1")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    from soy.services.model_resolver import resolve_model

    out = resolve_model("ollama/codestral")
    assert out["base_url"] == "http://cloud-ollama.example/v1"


def test_resolve_empty_string_raises():
    from soy.services.model_resolver import resolve_model

    with pytest.raises(ValueError):
        resolve_model("")


def test_resolve_cloud_missing_key_warns(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from soy.services.model_resolver import resolve_model

    with caplog.at_level("WARNING"):
        out = resolve_model("gpt-oss:cloud")
    # Empty key is still returned so the worker can fail later
    # with a useful error rather than blowing up here.
    assert out["api_key"] == ""
    assert out["is_cloud"] is True
    assert any("OLLAMA_API_KEY" in rec.message for rec in caplog.records)


def test_praisonai_agent_model_id_strips_prefix():
    from soy.services.model_resolver import praisonai_agent_model_id

    assert praisonai_agent_model_id("ollama/codestral") == "codestral"
    assert praisonai_agent_model_id("kimi-k2.6:cloud") == "kimi-k2.6"
    assert praisonai_agent_model_id("llama3.2") == "llama3.2"


def test_is_ollama_local():
    from soy.services.model_resolver import is_ollama_local

    assert is_ollama_local("ollama/codestral") is True
    assert is_ollama_local("llama3.2") is True
    assert is_ollama_local("kimi-k2.6:cloud") is False
    assert is_ollama_local("") is False


def test_export_env_for_praisonai(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OLLAMA_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from soy.services.model_resolver import export_env_for_praisonai

    env = export_env_for_praisonai("kimi-k2.6:cloud")
    assert env["OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert env["OPENAI_API_KEY"] == "sk-test"

    # No model argument — fall back to the env-derived values.
    env2 = export_env_for_praisonai()
    assert env2["OPENAI_BASE_URL"] == "http://localhost:11434/v1"
    assert env2["OPENAI_API_KEY"] == "sk-test"
