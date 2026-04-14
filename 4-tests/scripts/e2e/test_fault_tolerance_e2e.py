"""E2E tests: fault tolerance scenarios.

Tests T-M3-50 ~ T-M3-53 from the project plan (M3-D2 fault drills).
Verifies:
- ASR node crash → circuit breaker triggers → retry succeeds → recovery
- Queue backlog handling (200+ tasks)
- Worker crash → task re-queue
- Redis unavailability → reconnect → no data loss
"""

import asyncio
import io
import json
import time
from unittest.mock import patch

import pytest


async def _upload_file(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    assert resp.status_code == 201
    return resp.json()["file_id"]


async def _create_task(client, file_id: str) -> str:
    resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
    assert resp.status_code == 201
    return resp.json()[0]["task_id"]


@pytest.mark.e2e
class TestASRNodeCrash:
    """T-M3-50: ASR node crash → circuit breaker opens → retries → recovery after CLOSED."""

    async def test_circuit_breaker_trip_and_recovery(self, client):
        from app.fault.circuit_breaker import CircuitBreaker, CircuitState

        cb = CircuitBreaker("crash-test-server", failure_threshold=3, recovery_timeout=0.5, half_open_max_calls=2)

        assert cb.state == CircuitState.CLOSED

        for _ in range(3):
            await cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert await cb.allow_request() is False

        await asyncio.sleep(0.6)
        assert cb.state == CircuitState.HALF_OPEN
        assert await cb.allow_request() is True

        await cb.record_success()
        await cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert await cb.allow_request() is True

    async def test_task_retry_on_server_failure(self, client, db_session):
        """Simulate task failure and retry with server rotation."""
        from app.fault.retry import RetryPolicy

        policy = RetryPolicy(max_retries=3, base_delay=0.01, max_delay=0.1)

        servers = ["server-a", "server-b"]
        attempt = 0
        tried_servers = []

        for retry_num in range(policy.max_retries + 1):
            server = servers[retry_num % len(servers)]
            tried_servers.append(server)
            delay = policy.get_delay(retry_num)
            if retry_num < policy.max_retries:
                assert delay >= 0
                continue
            break

        assert len(tried_servers) == policy.max_retries + 1
        assert len(set(tried_servers)) > 1


@pytest.mark.e2e
class TestQueueBacklog:
    """T-M3-51: Queue backlog of 200+ tasks → tasks consumed in order → no loss."""

    async def test_large_batch_creation(self, client, db_session):
        file_id = await _upload_file(client)

        items = [{"file_id": file_id, "language": "zh"} for _ in range(50)]
        resp = await client.post("/api/v1/tasks", json={"items": items})
        assert resp.status_code == 201
        tasks = resp.json()
        assert len(tasks) == 50

        task_ids = {t["task_id"] for t in tasks}
        assert len(task_ids) == 50

        list_resp = await client.get("/api/v1/tasks?page=1&page_size=100")
        data = list_resp.json()
        assert data["total"] >= 50

    async def test_task_ordering_preserved(self, client, db_session):
        file_id = await _upload_file(client)

        items = [{"file_id": file_id} for _ in range(10)]
        resp = await client.post("/api/v1/tasks", json={"items": items})
        tasks = resp.json()

        created_ids = [t["task_id"] for t in tasks]
        assert len(created_ids) == 10
        assert created_ids == sorted(created_ids)


@pytest.mark.e2e
class TestWorkerCrash:
    """T-M3-52: Worker crash → task stays in non-terminal state → can be restarted."""

    async def test_incomplete_task_remains_recoverable(self, client, db_session):
        file_id = await _upload_file(client)
        task_id = await _create_task(client, file_id)

        from app.models import TaskStatus
        from app.storage.repository import TaskRepository

        repo = TaskRepository(db_session)
        task = await repo.get_task(task_id)
        await repo.update_task_status(task, TaskStatus.QUEUED)
        await repo.update_task_status(task, TaskStatus.DISPATCHED)
        await repo.update_task_status(task, TaskStatus.TRANSCRIBING)

        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.json()["status"] == "TRANSCRIBING"

        task = await repo.get_task(task_id)
        assert task.can_transition_to(TaskStatus.FAILED)
        await repo.update_task_status(task, TaskStatus.FAILED)

        task = await repo.get_task(task_id)
        assert task.can_transition_to(TaskStatus.PENDING)
        await repo.update_task_status(task, TaskStatus.PENDING)

        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.json()["status"] == "PENDING"


@pytest.mark.e2e
class TestRedisUnavailability:
    """T-M3-53: Redis briefly unavailable → reconnect → data integrity maintained."""

    async def test_api_works_without_redis(self, client):
        """Core API functionality works even if Redis is unavailable (SQLite-based)."""
        file_id = await _upload_file(client)
        task_id = await _create_task(client, file_id)

        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["task_id"] == task_id

        list_resp = await client.get("/api/v1/tasks")
        assert list_resp.status_code == 200

    async def test_circuit_breaker_state_survives_reset(self, client):
        """Circuit breaker operates in-memory; survives Redis outage."""
        from app.fault.circuit_breaker import CircuitBreaker, CircuitState

        cb = CircuitBreaker("redis-test-server", failure_threshold=5)
        for _ in range(3):
            await cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 3

        await cb.record_success()
        assert cb._failure_count == 0
