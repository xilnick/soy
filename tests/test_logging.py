"""Tests for structured JSON logging (soy.logging_config)."""

from __future__ import annotations

import json
import logging

from soy.logging_config import JsonFormatter, configure_logging, _HANDLER_MARKER


def _record(**extra):
    rec = logging.LogRecord(
        "soy.test", logging.INFO, __file__, 10, "hello %s", ("world",), None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_emits_valid_json():
    out = JsonFormatter().format(_record())
    d = json.loads(out)
    assert d["level"] == "INFO"
    assert d["logger"] == "soy.test"
    assert d["message"] == "hello world"
    assert "ts" in d and d["ts"].endswith("+00:00")


def test_json_formatter_includes_extra_fields():
    out = JsonFormatter().format(_record(mission_id="m-123", attempt=2))
    d = json.loads(out)
    assert d["mission_id"] == "m-123"
    assert d["attempt"] == 2


def test_json_formatter_extra_does_not_clobber_canonical_keys():
    # A caller attaching extra={"level": ...} / {"ts": ...} / {"logger": ...}
    # must NOT overwrite the canonical output values.
    out = JsonFormatter().format(
        _record(level="FAKE", ts="injected", logger="downstream", exc="x")
    )
    d = json.loads(out)
    assert d["level"] == "INFO"
    assert d["logger"] == "soy.test"
    assert d["ts"].endswith("+00:00")
    assert "exc" not in d  # no real exc_info → canonical exc absent, not "x"


def test_json_formatter_handles_unserialisable_extra():
    out = JsonFormatter().format(_record(obj=object()))
    d = json.loads(out)  # must not raise
    assert "obj" in d and isinstance(d["obj"], str)  # repr fallback


def test_json_formatter_renders_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord(
            "soy.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info(),
        )
    d = json.loads(JsonFormatter().format(rec))
    assert "exc" in d and "ValueError: boom" in d["exc"]


def _soy_handlers():
    return [
        h for h in logging.getLogger().handlers
        if getattr(h, _HANDLER_MARKER, False)
    ]


def test_configure_logging_installs_single_json_handler(monkeypatch):
    monkeypatch.setenv("SOY_LOG_FORMAT", "json")
    try:
        configure_logging()
        configure_logging()  # idempotent — must not stack handlers
        handlers = _soy_handlers()
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, JsonFormatter)
    finally:
        for h in _soy_handlers():
            logging.getLogger().removeHandler(h)


def test_configure_logging_text_mode(monkeypatch):
    monkeypatch.setenv("SOY_LOG_FORMAT", "text")
    try:
        configure_logging()
        handlers = _soy_handlers()
        assert len(handlers) == 1
        # Text mode does NOT use the JSON formatter.
        assert not isinstance(handlers[0].formatter, JsonFormatter)
    finally:
        for h in _soy_handlers():
            logging.getLogger().removeHandler(h)


def test_reset_logging_removes_handler(monkeypatch):
    from soy.logging_config import reset_logging

    monkeypatch.setenv("SOY_LOG_FORMAT", "json")
    configure_logging()
    assert _soy_handlers()  # installed
    reset_logging()
    assert _soy_handlers() == []  # cleanly removed
