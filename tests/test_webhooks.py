"""
Tests for the GitHub webhook + Git-as-SSOT service.

Covers signature verification (default-deny), the ``asf-run`` label
gate, idempotent ingestion through the shared
``create_mission_from_ingestion`` path, and the gated branch/spec git
step (run against a local temp repo — no network).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from soy.db import get_db
from soy.main import app
from soy.models.base import Base
import soy.models  # noqa: F401
from soy.models.mission import Mission


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def engine(tmp_path, monkeypatch):
    db_path = tmp_path / "asf_wh.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("ASF_DATABASE_URL", url)
    from soy import db as db_mod
    from soy.services.praisonai_worker import reset_worker

    reset_worker()
    db_mod.reset_engine()
    eng = create_engine(
        url, connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@pytest.fixture
def client(session_factory, monkeypatch) -> Iterator[TestClient]:
    def _get_db_override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    # The synchronous git step (when enabled) uses the same request
    # session, but the GitService is constructed via get_session... no:
    # it takes the live db. Still, keep the worker's session factory in
    # sync for safety.
    import soy.db as _asf_db
    monkeypatch.setattr(_asf_db, "get_session_local", lambda: session_factory)
    monkeypatch.setenv("ASF_RUN_MIGRATIONS_ON_STARTUP", "false")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def remote_repo(tmp_path):
    """A local git repo with one commit, usable as a clone source."""
    from git import Actor, Repo

    rp = tmp_path / "remote"
    rp.mkdir()
    repo = Repo.init(rp)
    (rp / "README.md").write_text("hello\n")
    repo.index.add(["README.md"])
    actor = Actor("seed", "seed@example.com")
    repo.index.commit("init", author=actor, committer=actor)
    return str(rp)


SECRET = "whsec-test"


def _issue_payload(number=42, labels=("asf-run",), repo_url="https://github.com/x/y.git", title="Add feature X", body="do the thing"):
    return {
        "action": "labeled",
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": n} for n in labels],
        },
        "repository": {"clone_url": repo_url, "html_url": "https://github.com/x/y"},
    }


def _post(client, payload, *, secret=SECRET, event="issues", sign=True):
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": event}
    if sign:
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = sig
    return client.post("/api/v1/webhooks/github", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Signature / default-deny
# ---------------------------------------------------------------------------
def test_webhook_rejects_missing_signature(client, monkeypatch):
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    r = _post(client, _issue_payload(), sign=False)
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_SIGNATURE"


def test_webhook_rejects_bad_signature(client, monkeypatch):
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    r = _post(client, _issue_payload(), secret="wrong-secret")
    assert r.status_code == 401


def test_webhook_default_deny_when_no_secret_configured(client, monkeypatch):
    monkeypatch.delenv("ASF_GITHUB_WEBHOOK_SECRET", raising=False)
    # Even a "correctly signed" request is denied — no secret = disabled.
    r = _post(client, _issue_payload())
    assert r.status_code == 401


def test_verify_signature_handles_nonascii_header(monkeypatch):
    """A non-ASCII signature header returns False, not a TypeError.

    HTTP clients normally send ASCII headers, but a raw socket client
    can send non-ASCII bytes (decoded latin-1 by the server) in the
    ``X-Hub-Signature-256`` value. ``hmac.compare_digest`` raises
    TypeError on such str inputs; the bytes comparison must not.
    """
    from soy.api.v1.webhooks import _verify_signature

    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    assert _verify_signature(b"body", "sha256=t\xe9ken-non-ascii") is False
    # Sanity: a correct signature still validates.
    good = "sha256=" + hmac.new(SECRET.encode(), b"body", hashlib.sha256).hexdigest()
    assert _verify_signature(b"body", good) is True


# ---------------------------------------------------------------------------
# Event / label gating
# ---------------------------------------------------------------------------
def test_webhook_ignores_non_issue_event(client, monkeypatch):
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    r = _post(client, {"zen": "hi"}, event="ping")
    assert r.status_code == 202
    assert r.json()["ignored"] is True


def test_webhook_ignores_unlabelled_issue(client, monkeypatch, session_factory):
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    r = _post(client, _issue_payload(labels=("bug",)))
    assert r.status_code == 202
    assert r.json()["ignored"] is True
    with session_factory() as db:
        assert db.query(Mission).count() == 0


def test_webhook_tolerates_malformed_payload(client, monkeypatch):
    """Non-object nested fields must not crash the handler with a 500."""
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    # issue is a string, repository is a list, labels absent → ignored,
    # not a 500.
    r = _post(client, {"issue": "not-an-object", "repository": []})
    assert r.status_code == 202
    assert r.json()["ignored"] is True

    # A non-object top-level payload → clean 400, not 500.
    body = b'["not", "an", "object"]'
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    r2 = client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": sig},
    )
    assert r2.status_code == 400
    assert r2.json()["code"] == "INVALID_PAYLOAD"


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def test_webhook_creates_mission_and_is_idempotent(client, monkeypatch, session_factory):
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.delenv("ASF_GIT_ENABLED", raising=False)  # git step off
    r1 = _post(client, _issue_payload(number=7))
    assert r1.status_code == 202, r1.text
    mid = r1.json()["mission_id"]
    assert "git" not in r1.json()  # gated off → genuine no-op

    # Re-delivery of the same issue → same mission (idempotent).
    r2 = _post(client, _issue_payload(number=7))
    assert r2.status_code == 202
    assert r2.json()["mission_id"] == mid
    with session_factory() as db:
        assert db.query(Mission).count() == 1
        m = db.get(Mission, uuid.UUID(mid))
        assert m.source == "github"
        assert m.external_id == "7"
        assert m.branch_prefix == "feature/asf-7"


# ---------------------------------------------------------------------------
# Git-as-SSOT step (gated)
# ---------------------------------------------------------------------------
def test_webhook_creates_branch_and_spec_when_git_enabled(
    client, monkeypatch, session_factory, remote_repo, tmp_path,
):
    monkeypatch.setenv("ASF_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ASF_GIT_ENABLED", "true")
    monkeypatch.setenv("ASF_GIT_WORKDIR", str(tmp_path / "work"))
    monkeypatch.delenv("ASF_GIT_PUSH_ENABLED", raising=False)  # no push

    r = _post(client, _issue_payload(number=9, repo_url=remote_repo))
    assert r.status_code == 202, r.text
    body = r.json()
    assert "git_error" not in body, body
    assert body["git"]["branch"] == "feature/asf-9"
    assert body["git"]["spec_path"] == "spec.md"
    assert body["git"]["pushed"] is False
    sha = body["git"]["commit_sha"]
    assert sha

    # DB pointers updated.
    with session_factory() as db:
        m = db.get(Mission, uuid.UUID(body["mission_id"]))
        assert m.branch == "feature/asf-9"
        assert m.spec_path == "spec.md"
        assert m.spec_commit_sha == sha

    # The spec file exists on the branch in the working clone.
    spec = tmp_path / "work" / body["mission_id"] / "spec.md"
    assert spec.exists()
    assert "RFC" in spec.read_text()


# ---------------------------------------------------------------------------
# GitService unit test (direct, against a local repo)
# ---------------------------------------------------------------------------
def test_git_service_creates_branch_and_commits_spec(
    session_factory, remote_repo, tmp_path, monkeypatch,
):
    from soy.api.v1.missions import create_mission_from_ingestion
    from soy.schemas import MissionCreate
    from soy.services.git_service import GitService

    monkeypatch.setenv("ASF_GIT_WORKDIR", str(tmp_path / "gwork"))
    with session_factory() as db:
        mission = create_mission_from_ingestion(db, MissionCreate(
            title="t", repo_url=remote_repo, branch_prefix="feature/asf-3",
            source="github", external_id="3",
        ))
        out = GitService(push_enabled=False).create_branch_and_spec(db, mission.id)
        assert out["branch"] == "feature/asf-3"
        assert out["commit_sha"]

        # Re-running is idempotent at the branch level (checks out the
        # existing branch, recommits the spec) — must not raise.
        out2 = GitService(push_enabled=False).create_branch_and_spec(db, mission.id)
        assert out2["branch"] == "feature/asf-3"
