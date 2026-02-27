"""Scheduler integration tests with database."""

import io

import pytest

from app.services.scheduler import TaskScheduler, ServerProfile


@pytest.mark.integration
class TestSchedulerIntegration:
    async def test_schedule_tasks_to_registered_server(self, client):
        """Create server and tasks, verify scheduling would assign correctly."""
        server_body = {
            "server_id": "sched-test-01",
            "host": "10.0.0.1",
            "port": 10095,
            "protocol_version": "v2_new",
            "max_concurrency": 4,
        }
        resp = await client.post("/api/v1/servers", json=server_body)
        assert resp.status_code == 201

        content = b"RIFF" + b"\x00" * 500
        files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
        upload_resp = await client.post("/api/v1/files/upload", files=files)
        file_id = upload_resp.json()["file_id"]

        task_resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
        assert task_resp.status_code == 201

        sched = TaskScheduler()
        servers = [ServerProfile(
            server_id="sched-test-01", host="10.0.0.1", port=10095,
            max_concurrency=4, rtf_baseline=0.3, status="ONLINE",
        )]
        tasks_to_schedule = [{"task_id": task_resp.json()[0]["task_id"], "audio_duration_sec": 120}]
        decisions = sched.schedule_batch(tasks_to_schedule, servers)
        assert len(decisions) == 1
        assert decisions[0].server_id == "sched-test-01"

    async def test_schedule_multi_server_load_balance(self, client):
        """Tasks should spread across multiple servers."""
        for i in range(2):
            await client.post("/api/v1/servers", json={
                "server_id": f"balance-{i}", "host": "10.0.0.1", "port": 10095 + i,
                "protocol_version": "v2_new", "max_concurrency": 2,
            })

        sched = TaskScheduler()
        servers = [
            ServerProfile(server_id="balance-0", host="10.0.0.1", port=10095, max_concurrency=2, rtf_baseline=0.3, status="ONLINE"),
            ServerProfile(server_id="balance-1", host="10.0.0.1", port=10096, max_concurrency=2, rtf_baseline=0.3, status="ONLINE"),
        ]
        tasks = [{"task_id": f"t{i}", "audio_duration_sec": 600} for i in range(4)]
        decisions = sched.schedule_batch(tasks, servers)
        assert len(decisions) == 4
        server_ids = {d.server_id for d in decisions}
        assert len(server_ids) == 2
