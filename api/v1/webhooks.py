"""
asf.api.v1.webhooks
===================

Inbound webhooks — currently the GitHub issue webhook that ingests a
mission.

  * ``POST /api/v1/webhooks/github``

Flow: verify the HMAC signature → require the ``asf-run`` label →
ingest the mission via the shared idempotent
:func:`asf.api.v1.missions.create_mission_from_ingestion` (so the
idempotency rules are not forked) → optionally run the Git-as-SSOT
branch/spec step. Signature verification is default-deny: with no
``ASF_GITHUB_WEBHOOK_SECRET`` configured, no request is trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from asf import config
from asf.api.v1.missions import create_mission_from_ingestion
from asf.db import get_db
from asf.errors import raise_http_error
from asf.schemas import MissionCreate

logger = logging.getLogger("asf.api.v1.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(body: bytes, header: str) -> bool:
    """Validate GitHub's ``X-Hub-Signature-256`` HMAC (default-deny).

    Returns False when no secret is configured (the webhook is then
    disabled — nothing is trusted) or the signature does not match.
    """
    secret = config.github_webhook_secret()
    if not secret:
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()
    # Compare as bytes: ``header`` is attacker-controlled and may carry
    # non-ASCII characters, on which ``hmac.compare_digest`` raises
    # TypeError for str inputs. Bytes comparison is constant-time and
    # accepts any input (a mismatch simply returns False).
    return hmac.compare_digest(
        expected.encode("ascii"), header.encode("utf-8"),
    )


@router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="GitHub issue webhook — ingest a mission",
)
async def github_webhook(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Ingest a mission from a labelled GitHub issue event.

    A mission is created only when the issue carries the configured
    trigger label (default ``asf-run``). Ingestion is idempotent on
    ``(source=github, external_id=issue number)`` so re-delivered
    deliveries return the same mission. When ``ASF_GIT_ENABLED`` is set
    the feature branch + ``spec.md`` are created as a best-effort step
    that never fails the webhook response.
    """
    body = await request.body()
    if not _verify_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        raise_http_error(
            status.HTTP_401_UNAUTHORIZED,
            "INVALID_SIGNATURE",
            "Missing or invalid webhook signature",
        )

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ignored": True, "reason": "ping"}
    if event != "issues":
        return {"ignored": True, "reason": f"unhandled event '{event}'"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_PAYLOAD",
            "Webhook body is not valid JSON",
        )
    if not isinstance(payload, dict):
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_PAYLOAD",
            "Webhook body must be a JSON object",
        )

    # The payload is attacker-controlled — coerce every nested shape so
    # a non-object issue/repository/label list can never crash the
    # handler with an unhandled 500.
    issue = payload.get("issue")
    issue = issue if isinstance(issue, dict) else {}
    raw_labels = issue.get("labels")
    labels = {
        lbl.get("name")
        for lbl in (raw_labels if isinstance(raw_labels, list) else [])
        if isinstance(lbl, dict)
    }
    trigger = config.asf_run_label()
    if trigger not in labels:
        return {"ignored": True, "reason": f"missing '{trigger}' label"}

    repo = payload.get("repository")
    repo = repo if isinstance(repo, dict) else {}
    repo_url = repo.get("clone_url") or repo.get("html_url")
    number = issue.get("number")
    if number is None or not repo_url:
        raise_http_error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_PAYLOAD",
            "issue.number and repository clone/html URL are required",
        )

    mc = MissionCreate(
        title=issue.get("title") or f"Issue #{number}",
        description=issue.get("body"),
        repo_url=repo_url,
        branch_prefix=f"feature/asf-{number}",
        source="github",
        external_id=str(number),
        issue_id=str(number),
    )
    mission = create_mission_from_ingestion(db, mc)
    result: dict = {
        "mission_id": str(mission.id),
        "status": (
            mission.status.value
            if hasattr(mission.status, "value")
            else str(mission.status)
        ),
    }

    # Best-effort Git-as-SSOT step. Gated off by default; never fails
    # the webhook. NOTE: this runs synchronously — with a slow remote a
    # real deployment should move the clone/commit to a background
    # worker, but for M2 it is gated (ASF_GIT_ENABLED) and the working
    # clone is local.
    if config.git_enabled() and mission.repo_url:
        try:
            from asf.services.git_service import GitService

            result["git"] = GitService().create_branch_and_spec(db, mission.id)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.exception(
                "Git-as-SSOT step failed for mission %s: %s", mission.id, exc,
            )
            result["git_error"] = str(exc)

    return result
