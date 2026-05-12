"""Task-group scheduling summary tests."""

import json

import pytest

from app.api.task_groups import _group_stats
from app.models import File, Task, TaskEvent, TaskSegment, TaskStatus, SegmentStatus


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

    assert stats["scheduling"] == {
        "work_steal_count": 2,
        "work_steal_estimated_gain_sec": 20.0,
        "est_stolen_total_sec": 14.0,
        "cross_server_segment_tasks": 1,
    }
