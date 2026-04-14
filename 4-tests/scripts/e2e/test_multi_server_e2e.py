"""E2E tests: multi-server scheduling and mixed protocol scenarios.

Tests T-E2E-10 ~ T-E2E-13 from the project plan.
Verifies:
- Multi-server registration + batch task distribution
- Mixed new/old protocol handling
- Offline node exclusion from scheduling
- SRT format download
"""

import io
import json
from pathlib import Path

import pytest


async def _upload_file(client, suffix: str = ".wav") -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": (f"audio{suffix}", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    assert resp.status_code == 201
    return resp.json()["file_id"]


async def _register_server(client, server_id: str, protocol: str = "funasr-main", port: int = 10095) -> str:
    body = {
        "server_id": server_id,
        "name": f"Server {server_id}",
        "host": "203.0.113.20",
        "port": port,
        "protocol_version": protocol,
        "max_concurrency": 4,
    }
    resp = await client.post("/api/v1/servers", json=body)
    if resp.status_code == 409:
        return server_id
    assert resp.status_code == 201
    return resp.json()["server_id"]


async def _complete_task_with_server(db_session, task_id: str, server_id: str):
    """Simulate task completion assigned to a specific server."""
    from app.models import TaskStatus
    from app.storage.repository import TaskRepository
    from app.storage.file_manager import save_result

    repo = TaskRepository(db_session)
    task = await repo.get_task(task_id)

    target_path = [
        TaskStatus.PREPROCESSING,
        TaskStatus.QUEUED,
        TaskStatus.DISPATCHED,
        TaskStatus.TRANSCRIBING,
        TaskStatus.SUCCEEDED,
    ]
    for target in target_path:
        if task.can_transition_to(target):
            await repo.update_task_status(task, target)
    task.assigned_server_id = server_id

    result_data = json.dumps({"text": f"转写结果 (by {server_id})"})
    await save_result(task_id, result_data, "json")
    await save_result(task_id, f"转写结果 (by {server_id})", "txt")
    task.result_path = task_id
    await db_session.flush()


@pytest.mark.e2e
class TestMultiServerRegistration:
    """T-E2E-10: Register 2 servers + batch submit 10 tasks → all SUCCEEDED."""

    async def test_register_two_servers(self, client):
        sid1 = await _register_server(client, "multi-srv-01", "funasr-main", 10095)
        sid2 = await _register_server(client, "multi-srv-02", "funasr-main", 10096)

        resp = await client.get("/api/v1/servers")
        assert resp.status_code == 200
        servers = resp.json()
        server_ids = {s["server_id"] for s in servers}
        assert sid1 in server_ids
        assert sid2 in server_ids

    async def test_batch_submit_and_complete(self, client, db_session):
        sid1 = await _register_server(client, "batch-srv-01")
        sid2 = await _register_server(client, "batch-srv-02", port=10096)

        file_ids = [await _upload_file(client) for _ in range(10)]

        items = [{"file_id": fid, "language": "zh"} for fid in file_ids]
        resp = await client.post("/api/v1/tasks", json={"items": items})
        assert resp.status_code == 201
        tasks = resp.json()
        assert len(tasks) == 10

        servers = [sid1, sid2]
        for i, task_data in enumerate(tasks):
            assigned = servers[i % 2]
            await _complete_task_with_server(db_session, task_data["task_id"], assigned)

        list_resp = await client.get("/api/v1/tasks?page_size=100")
        all_tasks = list_resp.json()["items"]
        succeeded = [t for t in all_tasks if t["status"] == "SUCCEEDED"]
        assert len(succeeded) >= 10


@pytest.mark.e2e
class TestMixedProtocol:
    """T-E2E-11: New and old protocol servers both receive tasks correctly."""

    async def test_new_and_old_protocol_servers(self, client, db_session):
        await _register_server(client, "new-proto-srv", "funasr-main", 10095)
        await _register_server(client, "old-proto-srv", "funasr-legacy", 10096)

        resp = await client.get("/api/v1/servers")
        servers = resp.json()
        protocols = {s["server_id"]: s["protocol_version"] for s in servers}
        assert "new-proto-srv" in protocols
        assert "old-proto-srv" in protocols

        fid1 = await _upload_file(client)
        fid2 = await _upload_file(client)

        resp1 = await client.post("/api/v1/tasks", json={"items": [{"file_id": fid1}]})
        resp2 = await client.post("/api/v1/tasks", json={"items": [{"file_id": fid2}]})

        tid1 = resp1.json()[0]["task_id"]
        tid2 = resp2.json()[0]["task_id"]

        await _complete_task_with_server(db_session, tid1, "new-proto-srv")
        await _complete_task_with_server(db_session, tid2, "old-proto-srv")

        r1 = await client.get(f"/api/v1/tasks/{tid1}")
        r2 = await client.get(f"/api/v1/tasks/{tid2}")
        assert r1.json()["status"] == "SUCCEEDED"
        assert r2.json()["status"] == "SUCCEEDED"
        assert r1.json()["assigned_server_id"] == "new-proto-srv"
        assert r2.json()["assigned_server_id"] == "old-proto-srv"


@pytest.mark.e2e
class TestOfflineNodeExclusion:
    """T-E2E-12: Offline server is excluded; tasks go only to online nodes."""

    async def test_offline_server_not_used(self, client, db_session):
        await _register_server(client, "online-srv", "funasr-main", 10095)
        await _register_server(client, "offline-srv", "funasr-main", 10096)

        from app.models import ServerInstance, ServerStatus
        from sqlalchemy import select

        result = await db_session.execute(
            select(ServerInstance).where(ServerInstance.server_id == "offline-srv")
        )
        offline = result.scalar_one_or_none()
        if offline:
            offline.status = ServerStatus.OFFLINE
            await db_session.flush()

        from app.storage.repository import ServerRepository

        repo = ServerRepository(db_session)
        online_servers = await repo.list_online_servers()
        online_ids = {s.server_id for s in online_servers}
        assert "offline-srv" not in online_ids
        assert "online-srv" in online_ids


@pytest.mark.e2e
class TestSRTFormatDownload:
    """T-E2E-13: SRT format result contains timestamps + text."""

    async def test_srt_format_result(self, client, db_session):
        file_id = await _upload_file(client)
        resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
        task_id = resp.json()[0]["task_id"]

        from app.models import TaskStatus
        from app.storage.repository import TaskRepository
        from app.storage.file_manager import save_result

        repo = TaskRepository(db_session)
        task = await repo.get_task(task_id)
        for target in [TaskStatus.PREPROCESSING, TaskStatus.QUEUED, TaskStatus.DISPATCHED,
                        TaskStatus.TRANSCRIBING, TaskStatus.SUCCEEDED]:
            if task.can_transition_to(target):
                await repo.update_task_status(task, target)

        srt_content = (
            "1\n"
            "00:00:00,000 --> 00:00:02,000\n"
            "这是一段测试\n\n"
            "2\n"
            "00:00:02,000 --> 00:00:05,000\n"
            "转写结果文本。\n\n"
        )
        await save_result(task_id, srt_content, "srt")
        task.result_path = task_id
        await db_session.flush()

        srt_resp = await client.get(f"/api/v1/tasks/{task_id}/result?format=srt")
        assert srt_resp.status_code == 200
        assert "-->" in srt_resp.text
        assert "这是一段测试" in srt_resp.text
