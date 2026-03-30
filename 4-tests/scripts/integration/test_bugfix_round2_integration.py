"""Integration tests for round 2 bug fixes.

Fix 2 (P2): Benchmark capacity comparison excludes offline servers
Fix 4 (P2): Batch JSON results return valid JSON array
"""

import io
import json

import pytest

from app.models import Task, TaskStatus
from app.storage.file_manager import save_result


async def _upload_file(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    return resp.json()["file_id"]


async def _create_batch_with_json_results(client, db_session, count=2):
    """Create tasks, mark SUCCEEDED, save JSON results."""
    from sqlalchemy import select

    file_ids = [await _upload_file(client) for _ in range(count)]
    body = {"items": [{"file_id": fid, "language": "zh"} for fid in file_ids]}
    resp = await client.post("/api/v1/tasks", json=body)
    tasks = resp.json()
    group_id = tasks[0]["task_group_id"]

    for i, t in enumerate(tasks):
        stmt = select(Task).where(Task.task_id == t["task_id"])
        task_obj = (await db_session.execute(stmt)).scalar_one()
        task_obj.status = TaskStatus.SUCCEEDED.value
        task_obj.progress = 1.0
        task_obj.result_path = t["task_id"]

        result_json = json.dumps({"text": f"转写结果第{i+1}段", "mode": "offline"})
        await save_result(t["task_id"], result_json, "json")
        await save_result(t["task_id"], f"转写结果第{i+1}段", "txt")

    await db_session.commit()
    return group_id, [t["task_id"] for t in tasks]


@pytest.mark.integration
class TestBatchJSONResultsAPI:
    """Fix 4: GET /task-groups/{id}/results?format=json must return valid JSON."""

    async def test_json_results_is_valid_json_array(self, client, db_session):
        group_id, _ = await _create_batch_with_json_results(client, db_session, 3)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=json")
        assert resp.status_code == 200
        assert "application/json" in resp.headers["content-type"]

        data = resp.json()
        assert isinstance(data, list), f"Expected JSON array, got {type(data)}"
        assert len(data) == 3

        for item in data:
            assert "task_id" in item
            assert "file_name" in item
            assert "result" in item
            assert isinstance(item["result"], dict)
            assert "text" in item["result"]

    async def test_json_results_single_task(self, client, db_session):
        group_id, _ = await _create_batch_with_json_results(client, db_session, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=json")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    async def test_txt_results_still_works(self, client, db_session):
        """txt format should still use the text concatenation approach."""
        group_id, _ = await _create_batch_with_json_results(client, db_session, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=txt")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "---" in resp.text


@pytest.mark.integration
class TestBenchmarkCapacityComparisonFiltering:
    """Fix 2: Capacity comparison should only include servers still ONLINE after benchmark."""

    async def test_unreachable_server_not_in_capacity_comparison(self, client, db_session):
        """Register unreachable server, force ONLINE, benchmark → should be excluded."""
        from sqlalchemy import select
        from app.models import ServerInstance, ServerStatus

        body = {
            "server_id": "asr-cap-filter-01",
            "name": "Unreachable Node",
            "host": "192.168.254.254",
            "port": 19998,
            "protocol_version": "v2_new",
            "max_concurrency": 4,
        }
        await client.post("/api/v1/servers", json=body)

        stmt = select(ServerInstance).where(ServerInstance.server_id == "asr-cap-filter-01")
        server = (await db_session.execute(stmt)).scalar_one()
        server.status = ServerStatus.ONLINE
        await db_session.commit()

        resp = await client.post("/api/v1/servers/benchmark")
        assert resp.status_code == 200
        data = resp.json()

        cap_server_ids = {c["server_id"] for c in data.get("capacity_comparison", [])}
        assert "asr-cap-filter-01" not in cap_server_ids

        await db_session.refresh(server)
        assert server.status == ServerStatus.OFFLINE
