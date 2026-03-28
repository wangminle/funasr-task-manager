"""Integration tests for batch results API endpoints (P1-2).

Tests zip download and multi-format result retrieval.
"""

import io
import zipfile

import pytest

from app.models import Task, TaskStatus
from app.storage.file_manager import save_result


async def _upload_file(client) -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    return resp.json()["file_id"]


async def _create_batch_with_results(client, db_session, count: int = 2):
    """Create a batch of tasks and manually set them to SUCCEEDED with result files."""
    file_ids = [await _upload_file(client) for _ in range(count)]
    body = {"items": [{"file_id": fid, "language": "zh"} for fid in file_ids]}
    resp = await client.post("/api/v1/tasks", json=body)
    tasks = resp.json()
    group_id = tasks[0]["task_group_id"]

    from sqlalchemy import select
    for t in tasks:
        stmt = select(Task).where(Task.task_id == t["task_id"])
        task_obj = (await db_session.execute(stmt)).scalar_one()
        task_obj.status = TaskStatus.SUCCEEDED.value
        task_obj.progress = 1.0
        task_obj.result_path = t["task_id"]

        await save_result(t["task_id"], '{"text": "测试转写结果"}', "json")
        await save_result(t["task_id"], "测试转写结果", "txt")
        await save_result(t["task_id"], "1\n00:00:00,000 --> 00:00:01,000\n测试\n", "srt")

    await db_session.commit()
    return group_id, [t["task_id"] for t in tasks]


@pytest.mark.integration
class TestBatchResultsText:
    async def test_get_batch_results_txt(self, client, db_session):
        group_id, _ = await _create_batch_with_results(client, db_session, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=txt")
        assert resp.status_code == 200
        assert "测试转写结果" in resp.text

    async def test_get_batch_results_json(self, client, db_session):
        group_id, _ = await _create_batch_with_results(client, db_session, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=json")
        assert resp.status_code == 200
        assert "text" in resp.text

    async def test_get_batch_results_srt(self, client, db_session):
        group_id, _ = await _create_batch_with_results(client, db_session, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=srt")
        assert resp.status_code == 200
        assert "00:00:00" in resp.text


@pytest.mark.integration
class TestBatchResultsZip:
    async def test_get_batch_results_zip(self, client, db_session):
        group_id, task_ids = await _create_batch_with_results(client, db_session, 2)
        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=zip")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        buf = io.BytesIO(resp.content)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert len(names) >= 2
            txt_files = [n for n in names if n.endswith(".txt")]
            json_files = [n for n in names if n.endswith(".json")]
            srt_files = [n for n in names if n.endswith(".srt")]
            assert len(txt_files) >= 1
            assert len(json_files) >= 1
            assert len(srt_files) >= 1

            for name in txt_files:
                content = zf.read(name).decode("utf-8")
                assert "测试转写结果" in content


@pytest.mark.integration
class TestBatchResultsEdgeCases:
    async def test_empty_group_results(self, client):
        resp = await client.get("/api/v1/task-groups/NONEXISTENT/results?format=txt")
        assert resp.status_code == 404

    async def test_no_succeeded_tasks(self, client):
        file_ids = [await _upload_file(client) for _ in range(2)]
        body = {"items": [{"file_id": fid} for fid in file_ids]}
        resp = await client.post("/api/v1/tasks", json=body)
        group_id = resp.json()[0]["task_group_id"]

        resp = await client.get(f"/api/v1/task-groups/{group_id}/results?format=txt")
        assert resp.status_code == 404
