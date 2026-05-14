"""Task API integration tests."""

import io

import pytest
from sqlalchemy import select
from ulid import ULID

from app.models import (
    File,
    SegmentStatus,
    ServerInstance,
    ServerStatus,
    Task,
    TaskSegment,
    TaskStatus,
)


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

    async def test_cancel_transcribing_task_releases_active_segments(self, db_session):
        from app.api.tasks import cancel_task

        file_id = str(ULID())
        task_id = str(ULID())
        segment_ids = [str(ULID()) for _ in range(4)]
        server_id = "asr-server-10097"

        db_session.add_all([
            File(
                file_id=file_id,
                user_id="default_user",
                original_name="long.wav",
                media_type="audio",
                mime="audio/wav",
                duration_sec=900.0,
                size_bytes=1024,
                storage_path="/tmp/long.wav",
            ),
            ServerInstance(
                server_id=server_id,
                name="10097",
                host="127.0.0.1",
                port=10097,
                protocol_version="funasr-ws",
                max_concurrency=4,
                status=ServerStatus.ONLINE.value,
                enabled=True,
            ),
            Task(
                task_id=task_id,
                user_id="default_user",
                file_id=file_id,
                status=TaskStatus.TRANSCRIBING.value,
                progress=0.5,
                language="zh",
            ),
        ])
        await db_session.flush()

        db_session.add_all([
            TaskSegment(
                segment_id=segment_ids[0],
                task_id=task_id,
                segment_index=0,
                source_start_ms=0,
                source_end_ms=1000,
                keep_start_ms=0,
                keep_end_ms=1000,
                storage_path="/tmp/seg0.wav",
                status=SegmentStatus.PENDING.value,
            ),
            TaskSegment(
                segment_id=segment_ids[1],
                task_id=task_id,
                segment_index=1,
                source_start_ms=1000,
                source_end_ms=2000,
                keep_start_ms=1000,
                keep_end_ms=2000,
                storage_path="/tmp/seg1.wav",
                status=SegmentStatus.DISPATCHED.value,
                assigned_server_id=server_id,
            ),
            TaskSegment(
                segment_id=segment_ids[2],
                task_id=task_id,
                segment_index=2,
                source_start_ms=2000,
                source_end_ms=3000,
                keep_start_ms=2000,
                keep_end_ms=3000,
                storage_path="/tmp/seg2.wav",
                status=SegmentStatus.TRANSCRIBING.value,
                assigned_server_id=server_id,
            ),
            TaskSegment(
                segment_id=segment_ids[3],
                task_id=task_id,
                segment_index=3,
                source_start_ms=3000,
                source_end_ms=4000,
                keep_start_ms=3000,
                keep_end_ms=4000,
                storage_path="/tmp/seg3.wav",
                status=SegmentStatus.SUCCEEDED.value,
                assigned_server_id=server_id,
                raw_result_json='{"text": "ok"}',
            ),
        ])
        await db_session.commit()

        response = await cancel_task(task_id, db_session, "default_user")

        assert response.status == TaskStatus.CANCELED.value

        rows = (await db_session.execute(
            select(TaskSegment).where(TaskSegment.task_id == task_id)
            .order_by(TaskSegment.segment_index.asc())
        )).scalars().all()

        assert [row.status for row in rows] == [
            SegmentStatus.FAILED.value,
            SegmentStatus.FAILED.value,
            SegmentStatus.FAILED.value,
            SegmentStatus.SUCCEEDED.value,
        ]
        assert rows[0].error_message == "Parent task canceled"
        assert rows[1].error_message == "Parent task canceled"
        assert rows[2].error_message == "Parent task canceled"
        assert rows[0].assigned_server_id is None
        assert rows[1].assigned_server_id is None
        assert rows[2].assigned_server_id is None
        assert rows[2].run_generation == 1


@pytest.mark.integration
class TestTaskResultAPI:
    async def test_incomplete_task_result_returns_409(self, client):
        file_id = await _upload_file(client)
        create_resp = await client.post("/api/v1/tasks", json={"items": [{"file_id": file_id}]})
        task_id = create_resp.json()[0]["task_id"]
        response = await client.get(f"/api/v1/tasks/{task_id}/result?format=json")
        assert response.status_code == 409
