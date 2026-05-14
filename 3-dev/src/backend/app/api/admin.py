"""Administrative operations for emergency recovery and diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.auth.token import verify_admin
from app.deps import DbSession
from app.models import ServerInstance, Task, TaskEvent, TaskSegment, TaskStatus, SegmentStatus

AdminUser = Annotated[str, Depends(verify_admin)]

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_ACTIVE_TASK_STATUSES = (
    TaskStatus.PREPROCESSING.value,
    TaskStatus.QUEUED.value,
    TaskStatus.DISPATCHED.value,
    TaskStatus.TRANSCRIBING.value,
)
_ACTIVE_SLOT_TASK_STATUSES = (
    TaskStatus.DISPATCHED.value,
    TaskStatus.TRANSCRIBING.value,
)
_ACTIVE_SEGMENT_STATUSES = (
    SegmentStatus.DISPATCHED.value,
    SegmentStatus.TRANSCRIBING.value,
)
_RELEASABLE_SEGMENT_STATUSES = (
    SegmentStatus.PENDING.value,
    SegmentStatus.DISPATCHED.value,
    SegmentStatus.TRANSCRIBING.value,
)
_TERMINAL_TASK_STATUSES = (
    TaskStatus.SUCCEEDED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELED.value,
)


@router.get("/active-slots")
async def active_slots(db: DbSession, admin: AdminUser) -> dict:
    """Return real server slot usage with task/segment sources."""
    servers = list((await db.execute(
        select(ServerInstance).order_by(ServerInstance.server_id.asc())
    )).scalars().all())

    active_segmented_task_ids = (
        select(TaskSegment.task_id).distinct()
        .where(TaskSegment.status.in_([
            SegmentStatus.PENDING.value,
            SegmentStatus.DISPATCHED.value,
            SegmentStatus.TRANSCRIBING.value,
            SegmentStatus.SUCCEEDED.value,
        ]))
    )
    whole_rows = list((await db.execute(
        select(Task)
        .where(
            Task.status.in_(_ACTIVE_SLOT_TASK_STATUSES),
            Task.assigned_server_id.is_not(None),
            Task.task_id.not_in(active_segmented_task_ids),
        )
        .order_by(Task.assigned_server_id.asc(), Task.created_at.asc())
    )).scalars().all())

    segment_rows = list((await db.execute(
        select(TaskSegment, Task)
        .join(Task, Task.task_id == TaskSegment.task_id)
        .where(
            TaskSegment.status.in_(_ACTIVE_SEGMENT_STATUSES),
            TaskSegment.assigned_server_id.is_not(None),
        )
        .order_by(TaskSegment.assigned_server_id.asc(), TaskSegment.created_at.asc())
    )).all())

    by_server: dict[str, dict] = {}
    for server in servers:
        by_server[server.server_id] = {
            "server_id": server.server_id,
            "host": server.host,
            "port": server.port,
            "status": server.status,
            "enabled": server.enabled,
            "max_concurrency": server.max_concurrency,
            "active_slots": 0,
            "whole_tasks": [],
            "segments": [],
        }

    def ensure_server(server_id: str) -> dict:
        return by_server.setdefault(server_id, {
            "server_id": server_id,
            "host": None,
            "port": None,
            "status": "UNKNOWN",
            "enabled": None,
            "max_concurrency": None,
            "active_slots": 0,
            "whole_tasks": [],
            "segments": [],
        })

    for task in whole_rows:
        server = ensure_server(task.assigned_server_id)
        server["active_slots"] += 1
        server["whole_tasks"].append({
            "task_id": task.task_id,
            "task_group_id": task.task_group_id,
            "status": task.status,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "is_zombie": False,
        })

    zombie_segments = 0
    for segment, task in segment_rows:
        server = ensure_server(segment.assigned_server_id)
        is_zombie = task.status in _TERMINAL_TASK_STATUSES
        if is_zombie:
            zombie_segments += 1
        server["active_slots"] += 1
        server["segments"].append({
            "segment_id": segment.segment_id,
            "task_id": segment.task_id,
            "task_group_id": task.task_group_id,
            "status": segment.status,
            "parent_status": task.status,
            "segment_index": segment.segment_index,
            "created_at": segment.created_at,
            "started_at": segment.started_at,
            "is_zombie": is_zombie,
        })

    server_list = list(by_server.values())
    total_active_slots = sum(s["active_slots"] for s in server_list)
    return {
        "total_active_slots": total_active_slots,
        "zombie_segments": zombie_segments,
        "servers": server_list,
        "requested_by": admin,
    }


@router.post("/emergency-stop")
async def emergency_stop(
    db: DbSession,
    admin: AdminUser,
    scope: Literal["all", "group"] = Query("all"),
    group_id: str | None = Query(None),
    dry_run: bool = Query(True),
    confirm: bool = Query(False),
) -> dict:
    """Cancel active work and release active segment slots.

    dry_run=true is the safe default. Mutating mode requires confirm=true.
    """
    if scope == "group" and not group_id:
        raise HTTPException(status_code=422, detail="group_id is required when scope=group")
    if not dry_run and not confirm:
        raise HTTPException(status_code=409, detail="emergency-stop requires confirm=true when dry_run=false")

    task_where = [Task.status.in_(_ACTIVE_TASK_STATUSES)]
    segment_task_where = []
    if scope == "group":
        task_where.append(Task.task_group_id == group_id)
        segment_task_where.append(Task.task_group_id == group_id)

    tasks = list((await db.execute(
        select(Task).where(*task_where).order_by(Task.created_at.asc())
    )).scalars().all())

    segment_rows = list((await db.execute(
        select(TaskSegment, Task)
        .join(Task, Task.task_id == TaskSegment.task_id)
        .where(
            TaskSegment.status.in_(_RELEASABLE_SEGMENT_STATUSES),
            *segment_task_where,
        )
        .order_by(TaskSegment.created_at.asc())
    )).all())

    slots_before = await active_slots(db, admin)
    result = {
        "scope": scope,
        "group_id": group_id,
        "dry_run": dry_run,
        "tasks_to_cancel": len(tasks),
        "segments_to_release": len(segment_rows),
        "active_slots_before": slots_before["total_active_slots"],
        "zombie_segments_before": slots_before["zombie_segments"],
        "tasks_canceled": 0,
        "segments_released": 0,
    }
    if dry_run:
        return result

    now = datetime.now(timezone.utc)
    from ulid import ULID

    for task in tasks:
        from_status = task.status
        task.run_generation += 1
        task.status = TaskStatus.CANCELED.value
        task.assigned_server_id = None
        task.completed_at = now
        db.add(TaskEvent(
            event_id=str(ULID()),
            task_id=task.task_id,
            from_status=from_status,
            to_status=TaskStatus.CANCELED.value,
            payload_json='{"event_type": "emergency_stop"}',
        ))
    for segment, _task in segment_rows:
        segment.run_generation += 1
        segment.status = SegmentStatus.FAILED.value
        segment.assigned_server_id = None
        segment.completed_at = now
        segment.error_message = "Emergency stop released active segment"

    await db.flush()
    result["tasks_canceled"] = len(tasks)
    result["segments_released"] = len(segment_rows)
    slots_after = await active_slots(db, admin)
    result["active_slots_after"] = slots_after["total_active_slots"]
    result["zombie_segments_after"] = slots_after["zombie_segments"]
    return result
