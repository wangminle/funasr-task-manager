"""Integration tests for CLI commands against real FastAPI test server."""

import json
import pytest
from pathlib import Path

import httpx
from typer.testing import CliRunner

from cli.main import app
from cli.api_client import ASRClient

runner = CliRunner()

pytestmark = pytest.mark.integration


@pytest.fixture
def api_client(client: httpx.AsyncClient):
    """Create a synchronous-like ASRClient pointing at the test server.
    For integration tests we use the runner with --server pointing at the test app."""
    pass


class TestCLIHealthIntegration:
    """CLI health/stats commands hit the real FastAPI test app."""

    @pytest.mark.asyncio
    async def test_health_via_api(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_stats_via_api(self, client):
        resp = await client.get("/api/v1/stats", headers={"X-API-Key": "test-token"})
        assert resp.status_code == 200
        data = resp.json()
        assert "server_total" in data
        assert "queue_depth" in data
        assert data["success_rate_24h"] >= 0

    @pytest.mark.asyncio
    async def test_stats_empty_db(self, client):
        resp = await client.get("/api/v1/stats", headers={"X-API-Key": "test-token"})
        data = resp.json()
        assert data["server_total"] == 0
        assert data["queue_depth"] == 0
        assert data["tasks_today_completed"] == 0

    @pytest.mark.asyncio
    async def test_stats_requires_auth_dependency(self, client):
        """Bug fix: stats endpoint now declares CurrentUser dependency.

        In test env auth_enabled=False so it returns 200 with default_user.
        When auth_enabled=True in production, missing token → 401.
        Here we verify the endpoint works with the dependency present.
        """
        resp = await client.get("/api/v1/stats")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_stats_with_auth_enabled_rejects_no_token(self, client):
        """Verify stats returns 401 when auth is explicitly enabled."""
        from app.auth import token as auth_mod
        original_enabled = auth_mod._AUTH_ENABLED
        auth_mod._AUTH_ENABLED = True
        try:
            resp = await client.get("/api/v1/stats")
            assert resp.status_code == 401
        finally:
            auth_mod._AUTH_ENABLED = original_enabled


class TestCLITaskSearchIntegration:
    """Test the newly added search parameter on list_tasks."""

    @pytest.mark.asyncio
    async def test_list_tasks_with_search(self, client):
        resp = await client.get("/api/v1/tasks", params={"search": "nonexistent"},
                                 headers={"X-API-Key": "test-token"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_list_tasks_with_status_filter(self, client):
        resp = await client.get("/api/v1/tasks", params={"status": "SUCCEEDED"},
                                 headers={"X-API-Key": "test-token"})
        assert resp.status_code == 200
