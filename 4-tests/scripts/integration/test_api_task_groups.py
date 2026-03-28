"""Task group (batch) API integration tests."""

import io

import pytest


async def _upload_file(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    return resp.json()["file_id"]


async def _create_batch(client, count: int = 3) -> tuple[str, list[str]]:
    """Create a batch of tasks and return (group_id, task_ids)."""
    file_ids = [await _upload_file(client) for _ in range(count)]
    body = {"items": [{"file_id": fid, "language": "zh"} for fid in file_ids]}
    resp = await client.post("/api/v1/tasks", json=body)
    assert resp.status_code == 201
    tasks = resp.json()
    group_id = tasks[0]["task_group_id"]
    assert group_id is not None
    task_ids = [t["task_id"] for t in tasks]
    return group_id, task_ids


@pytest.mark.integration
class TestTaskGroupOverview:
    async def test_get_group_overview(self, client):
        group_id, task_ids = await _create_batch(client, 3)
        resp = await client.get(f"/api/v1/task-groups/{group_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_group_id"] == group_id
        assert data["total"] == 3
        assert data["in_progress"] == 3
        assert data["is_complete"] is False

    async def test_get_nonexistent_group(self, client):
        resp = await client.get("/api/v1/task-groups/NONEXISTENT_00000000000")
        assert resp.status_code == 404


@pytest.mark.integration
class TestTaskGroupTaskList:
    async def test_list_group_tasks(self, client):
        group_id, task_ids = await _create_batch(client, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["task_group_id"] == group_id

    async def test_list_group_tasks_pagination(self, client):
        group_id, _ = await _create_batch(client, 3)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/tasks?page=1&page_size=2")
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 2


@pytest.mark.integration
class TestTaskGroupResults:
    async def test_no_succeeded_tasks_returns_404(self, client):
        group_id, _ = await _create_batch(client, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=txt")
        assert resp.status_code == 404


@pytest.mark.integration
class TestTaskGroupDelete:
    async def test_delete_group(self, client):
        group_id, task_ids = await _create_batch(client, 2)
        resp = await client.delete(f"/api/v1/task-groups/{group_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 2

        resp2 = await client.get(f"/api/v1/task-groups/{group_id}")
        assert resp2.status_code == 404

    async def test_delete_nonexistent_group(self, client):
        resp = await client.delete("/api/v1/task-groups/NONEXISTENT_00000000000")
        assert resp.status_code == 404


@pytest.mark.integration
class TestTaskListGroupFilter:
    async def test_list_tasks_with_group_filter(self, client):
        group_id, _ = await _create_batch(client, 3)
        # Also create a task without group
        fid = await _upload_file(client)
        await client.post("/api/v1/tasks", json={"items": [{"file_id": fid}]})

        resp = await client.get(f"/api/v1/tasks?group={group_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3


@pytest.mark.integration
class TestDiagnosticsAPI:
    async def test_diagnostics_endpoint(self, client):
        resp = await client.get("/api/v1/diagnostics")
        assert resp.status_code == 200
        data = resp.json()
        assert "checks" in data
        assert "has_blocking_errors" in data
        assert isinstance(data["checks"], list)
        assert len(data["checks"]) >= 3
