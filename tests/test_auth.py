"""Tests for soy.auth — API key authentication dependency."""

from __future__ import annotations

import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from soy.auth import verify_api_key


@pytest.fixture()
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure SOY_API_KEY is empty (auth disabled)."""
    monkeypatch.delenv("SOY_API_KEY", raising=False)


@pytest.fixture()
def _with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set SOY_API_KEY to a known value."""
    monkeypatch.setenv("SOY_API_KEY", "test-secret-key-123")


def _make_app() -> FastAPI:
    """Build a minimal app that uses the auth dependency."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict:
        return {"service": "soy"}

    @app.get("/api/v1/missions")
    async def missions() -> dict:
        return {"total": 0, "items": []}

    @app.get("/api/v1/control/status")
    async def control_status() -> dict:
        return {"status": "ok"}

    @app.get("/ws/missions/abc/events")
    async def ws_endpoint() -> dict:
        return {"type": "hello"}

    # Re-mount with auth dependency on the API surface.
    api_app = FastAPI(dependencies=[Depends(verify_api_key)])

    @api_app.get("/health")
    async def api_health() -> dict:
        return {"status": "ok"}

    @api_app.get("/api/v1/missions")
    async def api_missions() -> dict:
        return {"total": 0, "items": []}

    @api_app.get("/api/v1/control/status")
    async def api_control() -> dict:
        return {"status": "ok"}

    @api_app.get("/ws/missions/abc/events")
    async def api_ws() -> dict:
        return {"type": "hello"}

    @api_app.get("/")
    async def api_root() -> dict:
        return {"service": "soy"}

    return api_app


class TestAuthDisabled:
    """When SOY_API_KEY is empty, all requests pass through."""

    def test_health_passes_without_key(self, _no_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_api_passes_without_key(self, _no_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/missions")
        assert resp.status_code == 200

    def test_ws_passes_without_key(self, _no_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/ws/missions/abc/events")
        assert resp.status_code == 200


class TestAuthEnabled:
    """When SOY_API_KEY is set, API endpoints require Bearer token."""

    def test_api_rejected_without_header(self, _with_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/missions")
        assert resp.status_code == 401
        # Error body shape: {"detail": {"code": "MISSING_API_KEY", ...}}
        # or {"code": "MISSING_API_KEY", ...} depending on handler.
        body = resp.json()
        code = body.get("code") or (body.get("detail") or {}).get("code")
        assert code == "MISSING_API_KEY"

    def test_api_rejected_with_wrong_key(self, _with_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/v1/missions",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401
        body = resp.json()
        code = body.get("code") or (body.get("detail") or {}).get("code")
        assert code == "INVALID_API_KEY"

    def test_api_passes_with_correct_key(self, _with_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/v1/missions",
            headers={"Authorization": "Bearer test-secret-key-123"},
        )
        assert resp.status_code == 200

    def test_health_exempt_from_auth(self, _with_api_key: None) -> None:
        """Health endpoint is on the root app, not the API router."""
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        # Mount API router with auth dependency
        from fastapi import APIRouter
        api_router = APIRouter()

        @api_router.get("/api/v1/test")
        async def test_endpoint() -> dict:
            return {"ok": True}

        app.include_router(api_router, dependencies=[Depends(verify_api_key)])

        client = TestClient(app)
        # Health should pass without auth
        assert client.get("/health").status_code == 200
        # API endpoint should require auth
        assert client.get("/api/v1/test").status_code == 401

    def test_control_endpoint_requires_auth(self, _with_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/control/status")
        assert resp.status_code == 401

    def test_www_authenticate_header_present(self, _with_api_key: None) -> None:
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/missions")
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_auth_with_basic_scheme_rejected(self, _with_api_key: None) -> None:
        """Only Bearer scheme is accepted, not Basic."""
        app = _make_app()
        client = TestClient(app)
        resp = client.get(
            "/api/v1/missions",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        assert resp.status_code == 401
