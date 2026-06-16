"""
soy.logging_config
===================

Structured JSON logging for the ASF backend.

FastAPI/uvicorn run under PM2 in production; PM2 captures stdout line
by line. Emitting one JSON object per line makes the logs machine-
parseable (Mission Control / log shippers) while staying greppable.

Intentionally dependency-free — a ~30-line :class:`logging.Formatter`
subclass rather than ``structlog``/``python-json-logger`` — so the
backend gains no new runtime dependency for logging.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Attributes present on every ``logging.LogRecord``; anything *else* a
# caller attaches via ``logger.info(..., extra={...})`` is emitted as a
# top-level JSON field. Computed once from a probe record.
_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}

# The formatter's own output keys. These are NOT LogRecord attributes,
# so ``logging.makeRecord`` accepts them as ``extra=`` fields — without
# this guard a caller's ``extra={"level": ...}`` would silently
# overwrite the canonical value and corrupt the log schema.
_OUTPUT_KEYS = {"ts", "level", "logger", "message", "exc", "stack"}


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Always includes ``ts`` (UTC ISO-8601), ``level``, ``logger`` and
    ``message``. Exception info is rendered under ``exc``. Any
    JSON-serialisable ``extra=`` fields are merged in at the top level
    (non-serialisable values are ``repr``-ed so a bad extra never
    crashes logging).
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(
                record.created, timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in _OUTPUT_KEYS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        return json.dumps(payload, default=str)


_HANDLER_MARKER = "_asf_log_handler"


def configure_logging(fmt: str | None = None, level: str | None = None) -> None:
    """Install the ASF stdout log handler on the root logger.

    Idempotent: a previously-installed ASF handler is removed first, so
    calling this more than once (e.g. a re-entered FastAPI lifespan in
    tests) does not stack duplicate handlers. ``fmt`` defaults to
    :func:`soy.config.log_format` (``"json"`` unless overridden).
    """
    from soy import config

    fmt = (fmt or config.log_format()).lower()
    level = (level or config.log_level()).upper()

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_MARKER, True)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


def reset_logging() -> None:
    """Remove the ASF stdout handler installed by :func:`configure_logging`.

    Called from the FastAPI lifespan shutdown so the handler does not
    outlive the app. In production this is a no-op cleanup at process
    exit; in tests (where the lifespan is entered per ``TestClient``)
    it keeps the root logger from accumulating ASF handlers across the
    session.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
