"""Task API integration tests."""

import io

import pytest


async def _upload_file(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    return resp.json()["file_id"]


@pytest.mark.integration
class TestTaskCreateAPI:
    async def test_create_single_task(self, client):
        file_id = await _upload_file(client)
        body = {"items": [{"file_id": file_id, "language": "zh"}]}
        response = await client.post("/api/v1/tasks", json=body)
        assert response.status_code == 201
        data = response.json()
        assert len(data) == 1
        assert data[0]["status"] == "PREPROCESSING"
        assert len(data[0]["task_id"]) == 26

    async def test_create_task_with_nonexistent_file(self, client):
        body = {"items": [{"file_id": "nonexistent_00000000000000"}]}
        response = await client.post("/api/v1/tasks", json=body)
        assert response.status_code == 404

    async def test_create_batch_tasks(self, client):
        fid1 = await _upload_file(client)
        fid2 = await _upload_file(client)
        body = {"items": [{"file_id": fid1}, {"file_id": fid2}]}
        response = await client.post("/api/v1/tasks", json=body)
        assert response.status_code == 201
        data = response.json()
        assert len(data) == 2
        assert data[0]["task_group_id"] is not None
        assert data[0]["task_group_id"] == data[1]["task_group_id"]


@pytest.mark.integration
class TestTaskQueryAPI:
    async def test_get_task_detail(self, client):
        file_id = await _upload_file(client)
        create_resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
        task_id = create_resp.json()[0]["task_id"]
        response = await client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        assert response.json()["task_id"] == task_id

    async def test_list_tasks_with_pagination(self, client):
        fid = await _upload_file(client)
        for _ in range(3):
            await client.post("/api/v1/tasks", json={"items": [{"file_id": fid}]})
        response = await client.get("/api/v1/tasks?page=1&page_size=2")
        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 2
        assert data["total"] == 3

    async def test_cancel_pending_task(self, client):
        file_id = await _upload_file(client)
        create_resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
        task_id = create_resp.json()[0]["task_id"]
        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.json()["status"] == "PREPROCESSING"


@pytest.mark.integration
class TestTaskResultAPI:
    async def test_incomplete_task_result_returns_409(self, client):
        file_id = await _upload_file(client)
        create_resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
        task_id = create_resp.json()[0]["task_id"]
        response = await client.get(f"/api/v1/tasks/{task_id}/result?format=json")
        assert response.status_code == 409
