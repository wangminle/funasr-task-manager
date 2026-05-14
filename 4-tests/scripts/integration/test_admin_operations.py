"""Admin operation integration tests."""

from ulid import ULID

import pytest
from sqlalchemy import select

from app.models import (
    File,
    SegmentStatus,
    ServerInstance,
    ServerStatus,
    Task,
    TaskSegment,
    TaskStatus,
)


def _id() -> str:
    return str(ULID())


async def _seed_file(db_session, *, file_id: str, name: str = "audio.wav") -> None:
    db_session.add(File(
        file_id=file_id,
        user_id="default_user",
        original_name=name,
        media_type="audio",
        mime="audio/wav",
        duration_sec=900.0,
        size_bytes=1024,
        storage_path=f"/tmp/{name}",
    ))


async def _seed_server(db_session, *, server_id: str = "asr-server-10097") -> None:
    db_session.add(ServerInstance(
        server_id=server_id,
        name="10097",
        host="127.0.0.1",
        port=10097,
        protocol_version="funasr-ws",
        max_concurrency=4,
        status=ServerStatus.ONLINE.value,
        enabled=True,
    ))


@pytest.mark.integration
class TestAdminActiveSlots:
    async def test_active_slots_reports_zombie_segments(self, db_session):
        from app.api.admin import active_slots

        server_id = "asr-server-10097"
        file_id = _id()
        task_id = _id()
        segment_id = _id()
        await _seed_file(db_session, file_id=file_id)
        await _seed_server(db_session, server_id=server_id)
        db_session.add(Task(
            task_id=task_id,
            user_id="default_user",
            file_id=file_id,
            status=TaskStatus.CANCELED.value,
            progress=1.0,
            language="zh",
        ))
        await db_session.flush()
        db_session.add(TaskSegment(
            segment_id=segment_id,
            task_id=task_id,
            segment_index=0,
            source_start_ms=0,
            source_end_ms=1000,
            keep_start_ms=0,
            keep_end_ms=1000,
            storage_path="/tmp/seg0.wav",
            status=SegmentStatus.TRANSCRIBING.value,
            assigned_server_id=server_id,
        ))
        await db_session.commit()

        result = await active_slots(db_session, "admin")

        assert result["total_active_slots"] == 1
        assert result["zombie_segments"] == 1
        server = result["servers"][0]
        assert server["server_id"] == server_id
        assert server["active_slots"] == 1
        assert server["segments"][0]["parent_status"] == TaskStatus.CANCELED.value
        assert server["segments"][0]["is_zombie"] is True


@pytest.mark.integration
class TestAdminEmergencyStop:
    async def test_emergency_stop_dry_run_does_not_mutate(self, db_session):
        from app.api.admin import emergency_stop

        server_id = "asr-server-10097"
        file_id = _id()
        task_id = _id()
        segment_id = _id()
        await _seed_file(db_session, file_id=file_id)
        await _seed_server(db_session, server_id=server_id)
        db_session.add(Task(
            task_id=task_id,
            user_id="default_user",
            file_id=file_id,
            status=TaskStatus.TRANSCRIBING.value,
            progress=0.5,
            language="zh",
            assigned_server_id=server_id,
        ))
        await db_session.flush()
        db_session.add(TaskSegment(
            segment_id=segment_id,
            task_id=task_id,
            segment_index=0,
            source_start_ms=0,
            source_end_ms=1000,
            keep_start_ms=0,
            keep_end_ms=1000,
            storage_path="/tmp/seg0.wav",
            status=SegmentStatus.TRANSCRIBING.value,
            assigned_server_id=server_id,
        ))
        await db_session.commit()

        result = await emergency_stop(db_session, "admin", scope="all", dry_run=True, confirm=False)

        assert result["dry_run"] is True
        assert result["tasks_to_cancel"] == 1
        assert result["segments_to_release"] == 1
        assert result["active_slots_before"] == 1
        task = (await db_session.execute(select(Task).where(Task.task_id == task_id))).scalar_one()
        segment = (await db_session.execute(select(TaskSegment).where(TaskSegment.segment_id == segment_id))).scalar_one()
        assert task.status == TaskStatus.TRANSCRIBING.value
        assert segment.status == SegmentStatus.TRANSCRIBING.value
        assert segment.assigned_server_id == server_id

    async def test_emergency_stop_confirm_cancels_tasks_and_releases_segments(self, db_session):
        from app.api.admin import emergency_stop, active_slots

        server_id = "asr-server-10097"
        file_id = _id()
        task_id = _id()
        segment_id = _id()
        await _seed_file(db_session, file_id=file_id)
        await _seed_server(db_session, server_id=server_id)
        db_session.add(Task(
            task_id=task_id,
            user_id="default_user",
            file_id=file_id,
            task_group_id="group-1",
            status=TaskStatus.TRANSCRIBING.value,
            progress=0.5,
            language="zh",
            assigned_server_id=server_id,
        ))
        await db_session.flush()
        db_session.add(TaskSegment(
            segment_id=segment_id,
            task_id=task_id,
            segment_index=0,
            source_start_ms=0,
            source_end_ms=1000,
            keep_start_ms=0,
            keep_end_ms=1000,
            storage_path="/tmp/seg0.wav",
            status=SegmentStatus.TRANSCRIBING.value,
            assigned_server_id=server_id,
        ))
        await db_session.commit()

        result = await emergency_stop(db_session, "admin", scope="all", dry_run=False, confirm=True)

        assert result["dry_run"] is False
        assert result["tasks_canceled"] == 1
        assert result["segments_released"] == 1
        task = (await db_session.execute(select(Task).where(Task.task_id == task_id))).scalar_one()
        segment = (await db_session.execute(select(TaskSegment).where(TaskSegment.segment_id == segment_id))).scalar_one()
        assert task.status == TaskStatus.CANCELED.value
        assert task.assigned_server_id is None
        assert segment.status == SegmentStatus.FAILED.value
        assert segment.assigned_server_id is None

        slots = await active_slots(db_session, "admin")
        assert slots["total_active_slots"] == 0
