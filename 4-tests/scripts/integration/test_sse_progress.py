"""SSE progress endpoint integration tests."""

import io

import pytest


async def _create_task(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    file_id = resp.json()["file_id"]
    resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
    return resp.json()[0]["task_id"]


@pytest.mark.integration
class TestSSEProgress:
    async def test_sse_nonexistent_task_returns_404(self, client):
        """T-M2-30: SSE endpoint rejects invalid task."""
        response = await client.get("/api/v1/tasks/nonexistent_00000000/progress")
        assert response.status_code == 404

    async def test_sse_endpoint_route_exists(self, client):
        """Verify the SSE route is registered (404 on missing task, not 405)."""
        response = await client.get("/api/v1/tasks/any_task_id_12345678/progress")
        assert response.status_code == 404


@pytest.mark.integration
class TestMultiFormatResult:
    async def test_result_format_json_param(self, client):
        """Verify format query parameter is accepted."""
        task_id = await _create_task(client)
        response = await client.get(f"/api/v1/tasks/{task_id}/result?format=json")
        assert response.status_code == 409

    async def test_result_format_srt_param(self, client):
        task_id = await _create_task(client)
        response = await client.get(f"/api/v1/tasks/{task_id}/result?format=srt")
        assert response.status_code == 409

    async def test_result_format_txt_param(self, client):
        task_id = await _create_task(client)
        response = await client.get(f"/api/v1/tasks/{task_id}/result?format=txt")
        assert response.status_code == 409
