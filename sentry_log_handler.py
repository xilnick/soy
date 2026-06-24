"""Centralized Sentry log handler.

Attaches to the ROOT Python logger so every ``logger.error()``,
``logger.exception()`` and ``logger.critical()`` call automatically
ships a ``sentry_sdk.capture_message`` to Sentry.

Primary Sentry path: ``unhandled_exception_handler`` (in ``main.py``)
+ this logging handler.  Backup path: ``capture_exception()`` calls
in route ``try/except`` blocks (preserves service-level context tags).

Usage::

    from sentry_log_handler import install_sentry_log_handler

    # After init_sentry() in lifespan():
    install_sentry_log_handler()
"""

from __future__ import annotations

import logging
import os

import sentry_sdk

_installed = False


class SentryLogHandler(logging.Handler):
    """Forward log records to Sentry via ``sentry_sdk.capture_message``.

    Attach the full exception traceback when the record carries one
    (``logger.exception`` or explicit ``exc_info=True``).
    """

    def __init__(self, level: int = logging.ERROR) -> None:
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("logger", record.name)
                scope.set_extra("pathname", record.pathname)
                scope.set_extra("lineno", record.lineno)
                scope.set_extra("funcName", record.funcName)

                # Log record stores the (type, value, traceback) tuple;
                # pass it through so Sentry renders the full stack.
                exc_info = record.exc_info if record.exc_info else None

                sentry_sdk.capture_message(
                    record.getMessage(),
                    level=_sentry_level(record.levelno),
                    exc_info=exc_info,
                )
        except Exception:
            # Never break the app because of logging.
            pass


def _sentry_level(python_level: int) -> str:
    """Map Python log level to Sentry severity string."""
    if python_level >= logging.CRITICAL:
        return "fatal"
    if python_level >= logging.ERROR:
        return "error"
    if python_level >= logging.WARNING:
        return "warning"
    if python_level >= logging.INFO:
        return "info"
    return "debug"


def install_sentry_log_handler(level: int = logging.ERROR) -> None:
    """Attach :class:`SentryLogHandler` to the ROOT logger.

    Idempotent: calling more than once is harmless.  Skips when
    ``SENTRY_DSN`` is empty so the handler never fires while the
    SDK is not initialised.
    """
    global _installed

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return

    if _installed:
        return

    handler = SentryLogHandler(level=level)
    logging.getLogger().addHandler(handler)
    _installed = True
    logging.getLogger(__name__).info(
        "Sentry log handler installed (level=%s)",
        logging.getLevelName(level),
    )
