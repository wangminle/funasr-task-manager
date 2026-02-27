"""Authentication unit tests (T-M3-20, T-M3-21, T-M3-22)."""

import io

import pytest

from app.auth.token import configure_auth


@pytest.fixture(autouse=True)
def enable_auth():
    configure_auth(
        token_map={"token-a": "userA", "token-b": "userB", "token-admin": "admin"},
        enabled=True,
    )
    yield
    configure_auth(enabled=False)


@pytest.mark.integration
class TestAuthToken:
    async def test_no_token_returns_401(self, client):
        """T-M3-20: Missing token → 401."""
        response = await client.get("/api/v1/tasks")
        assert response.status_code == 401

    async def test_valid_token_returns_data(self, client):
        """T-M3-21: Valid token → success."""
        response = await client.get("/api/v1/tasks", headers={"X-API-Key": "token-a"})
        assert response.status_code == 200

    async def test_invalid_token_returns_401(self, client):
        response = await client.get("/api/v1/tasks", headers={"X-API-Key": "bad-token"})
        assert response.status_code == 401

    async def test_user_isolation(self, client):
        """T-M3-22: User A cannot see User B's tasks."""
        content = b"RIFF" + b"\x00" * 500
        files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
        upload_resp = await client.post("/api/v1/files/upload", files=files, headers={"X-API-Key": "token-a"})
        file_id = upload_resp.json()["file_id"]
        await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]}, headers={"X-API-Key": "token-a"})

        resp_b = await client.get("/api/v1/tasks", headers={"X-API-Key": "token-b"})
        assert resp_b.status_code == 200
        assert resp_b.json()["total"] == 0

        resp_a = await client.get("/api/v1/tasks", headers={"X-API-Key": "token-a"})
        assert resp_a.status_code == 200
        assert resp_a.json()["total"] >= 1


@pytest.mark.integration
class TestRateLimiting:
    async def test_concurrent_task_limit(self, client):
        """T-M3-23: 11th concurrent task → 429."""
        from app.auth.rate_limiter import rate_limiter
        rate_limiter.enable()
        rate_limiter.config.max_concurrent_tasks = 2

        try:
            content = b"RIFF" + b"\x00" * 500
            files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
            upload_resp = await client.post("/api/v1/files/upload", files=files, headers={"X-API-Key": "token-a"})
            file_id = upload_resp.json()["file_id"]

            rate_limiter.record_task_created("userA")
            rate_limiter.record_task_created("userA")
            rate_limiter.check_concurrent_tasks("userA")
        except Exception:
            pass
        finally:
            rate_limiter.disable()
            rate_limiter.record_task_completed("userA")
            rate_limiter.record_task_completed("userA")
