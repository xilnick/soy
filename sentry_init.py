"""Sentry SDK initialisation for the Soy backend.

Call ``init_sentry()`` early in the FastAPI lifespan. When
``SENTRY_DSN`` is empty or missing, the function is a no-op so the
app has zero new outbound dependencies unless explicitly enabled.
"""

from __future__ import annotations

import logging
import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)


def init_sentry(service: str = "soy-api") -> None:
    """Initialise Sentry SDK when ``SENTRY_DSN`` is set.

    Idempotent: calling more than once is harmless.
    """
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.warning("Sentry enabled: False (SENTRY_DSN unset)")
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENV", "production"),
        release=os.environ.get("SENTRY_RELEASE", service),
        traces_sample_rate=float(
            os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")
        ),
        integrations=[FastApiIntegration(), StarletteIntegration()],
        send_default_pii=False,
    )
    logger.warning(
        "Sentry enabled: True (service=%s, env=%s)",
        service,
        os.environ.get("SENTRY_ENV", "production"),
    )
