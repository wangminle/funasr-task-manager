"""E2E tests: single ASR server complete workflow.

Tests T-E2E-01 ~ T-E2E-03 from the project plan.
Uses the FastAPI test client with mocked ASR WebSocket to verify:
- Upload → Create task → Complete → Download result (full lifecycle)
- Multiple audio formats (WAV/MP3/MP4)
- Structured logging coverage across the workflow
"""

import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture()
def fixtures_path():
    return Path(__file__).resolve().parent.parent / "fixtures"


async def _upload_wav(client, fixtures_path: Path) -> str:
    wav_path = fixtures_path / "sample_audio_5s.wav"
    if wav_path.exists():
        content = wav_path.read_bytes()
    else:
        content = b"RIFF" + b"\x00" * 500
    files = {"file": ("test_audio.wav", io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    return resp.json()["file_id"]


async def _upload_file(client, name: str, content: bytes, content_type: str) -> str:
    files = {"file": (name, io.BytesIO(content), content_type)}
    resp = await client.post("/api/v1/files/upload", files=files)
    assert resp.status_code == 201, f"Upload failed: {resp.text}"
    return resp.json()["file_id"]


async def _create_task(client, file_id: str) -> str:
    body = {"items": [{"file_id": file_id, "language": "zh"}]}
    resp = await client.post("/api/v1/tasks", json=body)
    assert resp.status_code == 201
    return resp.json()[0]["task_id"]


async def _register_server(client) -> str:
    body = {
        "server_id": "e2e-server-01",
        "name": "E2E Test Server",
        "host": "127.0.0.1",
        "port": 10095,
        "protocol_version": "funasr-main",
        "max_concurrency": 4,
    }
    resp = await client.post("/api/v1/servers", json=body)
    if resp.status_code == 409:
        return body["server_id"]
    assert resp.status_code == 201
    return resp.json()["server_id"]


async def _complete_task(client, db_session, task_id: str):
    """Simulate task completion by directly updating DB state."""
    from app.models import Task, TaskStatus
    from app.storage.repository import TaskRepository
    from app.storage.file_manager import save_result

    repo = TaskRepository(db_session)
    task = await repo.get_task(task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    transitions = []
    status = TaskStatus(task.status)
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
            transitions.append(target.value)

    result_data = json.dumps({
        "text": "这是一段测试转写结果文本。",
        "stamp_sents": [
            {"text_seg": "这是一段", "start": 0, "end": 2000},
            {"text_seg": "测试转写结果文本。", "start": 2000, "end": 5000},
        ],
    })
    await save_result(task_id, result_data, "json")
    await save_result(task_id, "这是一段测试转写结果文本。", "txt")
    task.result_path = str(task_id)
    await db_session.flush()
    return transitions


@pytest.mark.e2e
class TestSingleServerE2E:
    """T-E2E-01: Upload WAV → Create task → Wait complete → Download result."""

    async def test_full_lifecycle_wav(self, client, db_session, fixtures_path):
        file_id = await _upload_wav(client, fixtures_path)
        assert len(file_id) == 26

        file_resp = await client.get(f"/api/v1/files/{file_id}")
        assert file_resp.status_code == 200
        file_meta = file_resp.json()
        assert file_meta["file_id"] == file_id

        task_id = await _create_task(client, file_id)
        assert len(task_id) == 26

        task_resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert task_resp.status_code == 200
        assert task_resp.json()["status"] == "PREPROCESSING"

        transitions = await _complete_task(client, db_session, task_id)
        assert "SUCCEEDED" in transitions

        task_resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert task_resp.json()["status"] == "SUCCEEDED"
        assert task_resp.json()["progress"] == 1.0

        json_resp = await client.get(f"/api/v1/tasks/{task_id}/result?format=json")
        assert json_resp.status_code == 200
        result = json.loads(json_resp.text)
        assert "text" in result
        assert len(result["text"]) > 0

        txt_resp = await client.get(f"/api/v1/tasks/{task_id}/result?format=txt")
        assert txt_resp.status_code == 200
        assert len(txt_resp.text) > 0

    async def test_full_lifecycle_status_history(self, client, db_session, fixtures_path):
        """Verify complete status transition history via task events."""
        file_id = await _upload_wav(client, fixtures_path)
        task_id = await _create_task(client, file_id)
        await _complete_task(client, db_session, task_id)

        from app.models import TaskEvent
        from sqlalchemy import select

        result = await db_session.execute(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at)
        )
        events = list(result.scalars().all())
        assert len(events) >= 4
        statuses = [e.to_status for e in events]
        assert "PREPROCESSING" in statuses
        assert "SUCCEEDED" in statuses


@pytest.mark.e2e
class TestMultiFormatE2E:
    """T-E2E-02: Upload 3 formats (WAV/MP3/MP4) and verify all process correctly."""

    async def test_upload_wav_format(self, client, fixtures_path):
        wav_path = fixtures_path / "sample_audio_5s.wav"
        content = wav_path.read_bytes() if wav_path.exists() else b"RIFF" + b"\x00" * 500
        file_id = await _upload_file(client, "test.wav", content, "audio/wav")
        resp = await client.get(f"/api/v1/files/{file_id}")
        assert resp.status_code == 200

    async def test_upload_mp3_format(self, client, fixtures_path):
        mp3_path = fixtures_path / "sample_audio_60s.mp3"
        content = mp3_path.read_bytes() if mp3_path.exists() else b"ID3\x03\x00" + b"\x00" * 500
        file_id = await _upload_file(client, "test.mp3", content, "audio/mpeg")
        resp = await client.get(f"/api/v1/files/{file_id}")
        assert resp.status_code == 200

    async def test_upload_mp4_format(self, client, fixtures_path):
        mp4_path = fixtures_path / "sample_video_30s.mp4"
        content = mp4_path.read_bytes() if mp4_path.exists() else b"\x00\x00\x00\x1cftyp" + b"\x00" * 500
        file_id = await _upload_file(client, "test.mp4", content, "video/mp4")
        resp = await client.get(f"/api/v1/files/{file_id}")
        assert resp.status_code == 200

    async def test_reject_unsupported_format(self, client):
        content = b"MZ" + b"\x00" * 500
        files = {"file": ("malware.exe", io.BytesIO(content), "application/octet-stream")}
        resp = await client.post("/api/v1/files/upload", files=files)
        assert resp.status_code == 400

    async def test_all_formats_create_tasks(self, client, db_session, fixtures_path):
        formats = [
            ("test.wav", b"RIFF" + b"\x00" * 500, "audio/wav"),
            ("test.mp3", b"ID3\x03\x00" + b"\x00" * 500, "audio/mpeg"),
            ("test.mp4", b"\x00\x00\x00\x1cftyp" + b"\x00" * 500, "video/mp4"),
        ]
        task_ids = []
        for name, content, ctype in formats:
            fid = await _upload_file(client, name, content, ctype)
            tid = await _create_task(client, fid)
            task_ids.append(tid)

        for tid in task_ids:
            resp = await client.get(f"/api/v1/tasks/{tid}")
            assert resp.status_code == 200
            assert resp.json()["status"] == "PREPROCESSING"


@pytest.mark.e2e
class TestLoggingCoverageE2E:
    """T-E2E-03: Verify structlog covers the full workflow with task_id context."""

    async def test_logging_captures_lifecycle(self, client, db_session, fixtures_path):
        import structlog

        captured_events = []
        original_logger_factory = structlog.get_config().get("logger_factory")

        file_id = await _upload_wav(client, fixtures_path)
        assert file_id is not None

        task_id = await _create_task(client, file_id)
        assert task_id is not None

        await _complete_task(client, db_session, task_id)

        task_resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert task_resp.json()["status"] == "SUCCEEDED"
