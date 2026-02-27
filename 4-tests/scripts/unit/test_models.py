"""Database model unit tests."""

import pytest
from ulid import ULID

from app.models import (
    File, Task, TaskEvent, ServerInstance, CallbackOutbox,
    TaskStatus, ServerStatus, OutboxStatus, VALID_TRANSITIONS,
)


def _make_ulid() -> str:
    return str(ULID())


@pytest.mark.unit
class TestFileModel:
    async def test_file_crud(self, db_session):
        fid = _make_ulid()
        f = File(file_id=fid, user_id="user1", original_name="test.wav", size_bytes=1024, storage_path="/uploads/test.wav", status="UPLOADED")
        db_session.add(f)
        await db_session.flush()
        result = await db_session.get(File, fid)
        assert result is not None
        assert result.file_id == fid
        assert result.original_name == "test.wav"
        assert len(fid) == 26

    async def test_file_update(self, db_session):
        fid = _make_ulid()
        f = File(file_id=fid, user_id="user1", original_name="a.mp3", size_bytes=2048, storage_path="/x")
        db_session.add(f)
        await db_session.flush()
        f.status = "META_READY"
        f.duration_sec = 30.5
        f.sample_rate = 16000
        await db_session.flush()
        result = await db_session.get(File, fid)
        assert result.status == "META_READY"
        assert result.duration_sec == pytest.approx(30.5)

    async def test_file_delete(self, db_session):
        fid = _make_ulid()
        f = File(file_id=fid, user_id="user1", original_name="del.wav", size_bytes=100, storage_path="/d")
        db_session.add(f)
        await db_session.flush()
        await db_session.delete(f)
        await db_session.flush()
        result = await db_session.get(File, fid)
        assert result is None


@pytest.mark.unit
class TestTaskModel:
    async def test_task_crud_with_fk(self, db_session):
        fid, tid = _make_ulid(), _make_ulid()
        f = File(file_id=fid, user_id="user1", original_name="t.wav", size_bytes=500, storage_path="/u")
        t = Task(task_id=tid, user_id="user1", file_id=fid, status=TaskStatus.PENDING)
        db_session.add_all([f, t])
        await db_session.flush()
        result = await db_session.get(Task, tid)
        assert result is not None
        assert result.file_id == fid

    async def test_task_status_machine_valid(self, db_session):
        fid, tid = _make_ulid(), _make_ulid()
        f = File(file_id=fid, user_id="u", original_name="x.wav", size_bytes=1, storage_path="/p")
        t = Task(task_id=tid, user_id="u", file_id=fid, status=TaskStatus.PENDING)
        db_session.add_all([f, t])
        await db_session.flush()
        t.transition_to(TaskStatus.PREPROCESSING)
        assert t.status == TaskStatus.PREPROCESSING
        assert t.progress == pytest.approx(0.05)
        t.transition_to(TaskStatus.QUEUED)
        t.transition_to(TaskStatus.DISPATCHED)
        t.transition_to(TaskStatus.TRANSCRIBING)
        t.transition_to(TaskStatus.SUCCEEDED)
        assert t.status == TaskStatus.SUCCEEDED
        assert t.progress == pytest.approx(1.0)
        assert t.completed_at is not None

    async def test_task_status_machine_invalid(self, db_session):
        fid, tid = _make_ulid(), _make_ulid()
        f = File(file_id=fid, user_id="u", original_name="x.wav", size_bytes=1, storage_path="/p")
        t = Task(task_id=tid, user_id="u", file_id=fid, status=TaskStatus.SUCCEEDED)
        db_session.add_all([f, t])
        await db_session.flush()
        with pytest.raises(ValueError, match="Invalid transition"):
            t.transition_to(TaskStatus.PENDING)

    async def test_task_cancel_from_pending(self, db_session):
        fid, tid = _make_ulid(), _make_ulid()
        f = File(file_id=fid, user_id="u", original_name="x.wav", size_bytes=1, storage_path="/p")
        t = Task(task_id=tid, user_id="u", file_id=fid, status=TaskStatus.PENDING)
        db_session.add_all([f, t])
        await db_session.flush()
        t.transition_to(TaskStatus.CANCELED)
        assert t.status == TaskStatus.CANCELED


@pytest.mark.unit
class TestTaskEventModel:
    async def test_task_event_write(self, db_session):
        fid, tid, eid = _make_ulid(), _make_ulid(), _make_ulid()
        f = File(file_id=fid, user_id="u", original_name="x.wav", size_bytes=1, storage_path="/p")
        t = Task(task_id=tid, user_id="u", file_id=fid, status=TaskStatus.PENDING)
        e = TaskEvent(event_id=eid, task_id=tid, from_status=TaskStatus.PENDING, to_status=TaskStatus.PREPROCESSING, payload_json='{"reason": "test"}')
        db_session.add_all([f, t, e])
        await db_session.flush()
        result = await db_session.get(TaskEvent, eid)
        assert result is not None
        assert result.from_status == TaskStatus.PENDING
        assert len(result.event_id) == 26


@pytest.mark.unit
class TestServerInstanceModel:
    async def test_server_crud(self, db_session):
        s = ServerInstance(server_id="asr-01", host="192.168.1.100", port=10095, protocol_version="v2_new", max_concurrency=4, status=ServerStatus.ONLINE)
        db_session.add(s)
        await db_session.flush()
        result = await db_session.get(ServerInstance, "asr-01")
        assert result is not None
        assert result.is_available()
        result.status = ServerStatus.OFFLINE
        await db_session.flush()
        assert not result.is_available()


@pytest.mark.unit
class TestCallbackOutboxModel:
    async def test_outbox_write(self, db_session):
        fid, tid, eid, oid = _make_ulid(), _make_ulid(), _make_ulid(), _make_ulid()
        f = File(file_id=fid, user_id="u", original_name="x.wav", size_bytes=1, storage_path="/p")
        t = Task(task_id=tid, user_id="u", file_id=fid)
        e = TaskEvent(event_id=eid, task_id=tid, to_status="SUCCEEDED")
        o = CallbackOutbox(outbox_id=oid, task_id=tid, event_id=eid, callback_url="https://example.com/hook", payload_json='{"status":"SUCCEEDED"}', status=OutboxStatus.PENDING)
        db_session.add_all([f, t, e, o])
        await db_session.flush()
        result = await db_session.get(CallbackOutbox, oid)
        assert result is not None
        assert result.status == OutboxStatus.PENDING
