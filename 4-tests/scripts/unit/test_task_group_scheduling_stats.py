"""Task-group scheduling summary tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.api.task_groups import _group_stats
from app.models import (
    File, ServerInstance, Task, TaskEvent, TaskSegment, TaskStatus, SegmentStatus,
)


@pytest.mark.unit
async def test_group_stats_include_work_steal_and_cross_server_segment_metrics(db_session):
    db_session.add(File(
        file_id="file-parent",
        user_id="test",
        original_name="parent.wav",
        storage_path="/tmp/parent.wav",
        size_bytes=100,
        duration_sec=1200,
        status="META_READY",
    ))
    db_session.add(Task(
        task_id="task-parent",
        user_id="test",
        file_id="file-parent",
        task_group_id="grp-1",
        status=TaskStatus.SUCCEEDED,
        progress=1.0,
    ))
    db_session.add_all([
        TaskSegment(
            segment_id="seg-1",
            task_id="task-parent",
            segment_index=0,
            source_start_ms=0,
            source_end_ms=600000,
            keep_start_ms=0,
            keep_end_ms=600000,
            storage_path="/tmp/seg-1.wav",
            status=SegmentStatus.SUCCEEDED,
            assigned_server_id="srv-1",
        ),
        TaskSegment(
            segment_id="seg-2",
            task_id="task-parent",
            segment_index=1,
            source_start_ms=600000,
            source_end_ms=1200000,
            keep_start_ms=600000,
            keep_end_ms=1200000,
            storage_path="/tmp/seg-2.wav",
            status=SegmentStatus.SUCCEEDED,
            assigned_server_id="srv-2",
        ),
        TaskEvent(
            event_id="evt-steal-1",
            task_id="task-parent",
            from_status=None,
            to_status=TaskStatus.DISPATCHED,
            payload_json=json.dumps({
                "event_type": "work_steal",
                "estimated_gain_sec": 12.5,
                "est_stolen_sec": 8.0,
            }),
        ),
        TaskEvent(
            event_id="evt-steal-2",
            task_id="task-parent",
            from_status=None,
            to_status=TaskStatus.DISPATCHED,
            payload_json=json.dumps({
                "event_type": "work_steal",
                "estimated_gain_sec": 7.5,
                "est_stolen_sec": 6.0,
            }),
        ),
    ])
    await db_session.commit()

    stats = await _group_stats(db_session, "grp-1", "test")

    sched = stats["scheduling"]
    assert sched["work_steal_count"] == 2
    assert sched["work_steal_estimated_gain_sec"] == 20.0
    assert sched["est_stolen_total_sec"] == 14.0
    assert sched["cross_server_segment_tasks"] == 1
    assert "idle_slot_seconds" in sched


@pytest.mark.unit
async def test_idle_slot_seconds_counts_fully_idle_available_servers(db_session):
    start = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    end = start + timedelta(seconds=100)

    db_session.add_all([
        ServerInstance(
            server_id="srv-busy",
            host="127.0.0.1",
            port=10095,
            protocol_version="v2_new",
            max_concurrency=1,
            status="ONLINE",
            enabled=True,
        ),
        ServerInstance(
            server_id="srv-idle",
            host="127.0.0.1",
            port=10096,
            protocol_version="v2_new",
            max_concurrency=1,
            status="ONLINE",
            enabled=True,
        ),
        File(
            file_id="file-one",
            user_id="test",
            original_name="one.wav",
            storage_path="/tmp/one.wav",
            size_bytes=100,
            duration_sec=100,
            status="META_READY",
        ),
        Task(
            task_id="task-one",
            user_id="test",
            file_id="file-one",
            task_group_id="grp-idle",
            status=TaskStatus.SUCCEEDED,
            progress=1.0,
            assigned_server_id="srv-busy",
            started_at=start,
            completed_at=end,
        ),
    ])
    await db_session.commit()

    stats = await _group_stats(db_session, "grp-idle", "test")

    assert stats["scheduling"]["idle_slot_seconds"] == 100.0
