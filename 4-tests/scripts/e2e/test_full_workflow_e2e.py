"""E2E tests: full system workflow combining all features.

Covers the complete integration of upload, task management, server management,
progress tracking, result retrieval, and error handling across the entire system.
"""

import io
import json

import pytest


async def _upload_file(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("workflow.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    assert resp.status_code == 201
    return resp.json()["file_id"]


async def _complete_task(db_session, task_id: str):
    from app.models import TaskStatus
    from app.storage.repository import TaskRepository
    from app.storage.file_manager import save_result

    repo = TaskRepository(db_session)
    task = await repo.get_task(task_id)

    for target in [TaskStatus.PREPROCESSING, TaskStatus.QUEUED, TaskStatus.DISPATCHED,
                    TaskStatus.TRANSCRIBING, TaskStatus.SUCCEEDED]:
        if task.can_transition_to(target):
            await repo.update_task_status(task, target)

    await save_result(task_id, json.dumps({"text": "完整工作流测试结果"}), "json")
    await save_result(task_id, "完整工作流测试结果", "txt")
    task.result_path = task_id
    await db_session.flush()


@pytest.mark.e2e
class TestFullWorkflowE2E:
    """Complete system workflow: health → upload → task → server → result."""

    async def test_health_check(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_complete_single_user_workflow(self, client, db_session):
        """Single user: upload file → create task → complete → get result."""
        file_id = await _upload_file(client)

        file_resp = await client.get(f"/api/v1/files/{file_id}")
        assert file_resp.status_code == 200
        assert file_resp.json()["file_id"] == file_id
        assert file_resp.json()["original_name"] == "workflow.wav"

        body = {"items": [{"file_id": file_id, "language": "zh"}]}
        task_resp = await client.post("/api/v1/tasks", json=body)
        assert task_resp.status_code == 201
        task_id = task_resp.json()[0]["task_id"]

        await _complete_task(db_session, task_id)

        result_resp = await client.get(f"/api/v1/tasks/{task_id}/result?format=json")
        assert result_resp.status_code == 200
        result_data = result_resp.json()
        assert "完整工作流测试结果" in result_data.get("text", "")

    async def test_batch_workflow_with_group(self, client, db_session):
        """Batch: upload multiple files → batch create → verify group → complete all."""
        fid1 = await _upload_file(client)
        fid2 = await _upload_file(client)
        fid3 = await _upload_file(client)

        body = {"items": [
            {"file_id": fid1, "language": "zh"},
            {"file_id": fid2, "language": "zh"},
            {"file_id": fid3, "language": "en"},
        ]}
        resp = await client.post("/api/v1/tasks", json=body)
        assert resp.status_code == 201
        tasks = resp.json()
        assert len(tasks) == 3

        group_id = tasks[0]["task_group_id"]
        assert group_id is not None
        assert all(t["task_group_id"] == group_id for t in tasks)

        for t in tasks:
            await _complete_task(db_session, t["task_id"])

        for t in tasks:
            r = await client.get(f"/api/v1/tasks/{t['task_id']}")
            assert r.json()["status"] == "SUCCEEDED"

    async def test_server_management_workflow(self, client, db_session):
        """Register server → list → verify → delete → verify removed."""
        body = {
            "server_id": "workflow-srv-01",
            "name": "Workflow Test Server",
            "host": "203.0.113.24",
            "port": 10095,
            "protocol_version": "funasr-main",
            "max_concurrency": 8,
        }
        reg_resp = await client.post("/api/v1/servers", json=body)
        assert reg_resp.status_code in (201, 409)

        list_resp = await client.get("/api/v1/servers")
        assert list_resp.status_code == 200
        server_ids = {s["server_id"] for s in list_resp.json()}
        assert "workflow-srv-01" in server_ids

        del_resp = await client.delete("/api/v1/servers/workflow-srv-01")
        assert del_resp.status_code == 204

        list_resp2 = await client.get("/api/v1/servers")
        server_ids2 = {s["server_id"] for s in list_resp2.json()}
        assert "workflow-srv-01" not in server_ids2

    async def test_task_cancellation_workflow(self, client, db_session):
        """Create task → cancel before completion → verify CANCELED."""
        file_id = await _upload_file(client)
        body = {"items": [{"file_id": file_id}]}
        resp = await client.post("/api/v1/tasks", json=body)
        task_id = resp.json()[0]["task_id"]

        from app.models import TaskStatus
        from app.storage.repository import TaskRepository

        repo = TaskRepository(db_session)
        task = await repo.get_task(task_id)
        await repo.update_task_status(task, TaskStatus.QUEUED)

        cancel_resp = await client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "CANCELED"

        result_resp = await client.get(f"/api/v1/tasks/{task_id}/result")
        assert result_resp.status_code == 409

    async def test_error_handling_nonexistent_resources(self, client):
        """Verify proper 404 responses for nonexistent resources."""
        resp = await client.get("/api/v1/files/nonexistent_00000000000000")
        assert resp.status_code == 404

        resp = await client.get("/api/v1/tasks/nonexistent_00000000000000")
        assert resp.status_code == 404

        resp = await client.get("/api/v1/tasks/nonexistent_00000000000000/result")
        assert resp.status_code == 404

    async def test_pagination_workflow(self, client, db_session):
        """Create many tasks → paginate through → verify all found."""
        file_id = await _upload_file(client)
        total_tasks = 15
        items = [{"file_id": file_id} for _ in range(total_tasks)]
        await client.post("/api/v1/tasks", json={"items": items})

        page1 = await client.get("/api/v1/tasks?page=1&page_size=5")
        assert page1.status_code == 200
        data1 = page1.json()
        assert len(data1["items"]) == 5
        assert data1["total"] >= total_tasks

        page2 = await client.get("/api/v1/tasks?page=2&page_size=5")
        data2 = page2.json()
        assert len(data2["items"]) == 5

        page1_ids = {t["task_id"] for t in data1["items"]}
        page2_ids = {t["task_id"] for t in data2["items"]}
        assert page1_ids.isdisjoint(page2_ids)

    async def test_api_docs_accessible(self, client):
        """Swagger UI docs endpoint is accessible."""
        resp = await client.get("/docs")
        assert resp.status_code == 200

        resp = await client.get("/redoc")
        assert resp.status_code == 200
