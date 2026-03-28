"""Task group (batch) management API endpoints."""

import io
import json as _json
import zipfile

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import delete as sql_delete, func, select

from app.deps import CurrentUser, DbSession
from app.models import File, Task, TaskEvent, TaskStatus
from app.storage.file_manager import read_result
from app.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/task-groups", tags=["task-groups"])


@router.get("/{group_id}")
async def get_task_group(group_id: str, db: DbSession, user_id: CurrentUser):
    """Get batch overview: total, completed, failed, progress."""
    stats = await _group_stats(db, group_id, user_id)
    if stats["total"] == 0:
        raise HTTPException(status_code=404, detail="Task group not found")
    return stats


@router.get("/{group_id}/tasks")
async def list_group_tasks(
    group_id: str, db: DbSession, user_id: CurrentUser,
    page: int = Query(1, ge=1), page_size: int = Query(100, ge=1, le=500),
):
    """List all tasks in a batch."""
    base = select(Task).where(Task.task_group_id == group_id, Task.user_id == user_id)
    count_stmt = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_stmt)).scalar() or 0
    if total == 0:
        raise HTTPException(status_code=404, detail="Task group not found")

    stmt = base.order_by(Task.created_at.asc()).offset((page - 1) * page_size).limit(page_size)
    tasks = list((await db.execute(stmt)).scalars().all())

    from app.schemas.task import TaskResponse
    return {
        "task_group_id": group_id,
        "items": [TaskResponse.model_validate(t) for t in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{group_id}/results")
async def get_group_results(
    group_id: str, db: DbSession, user_id: CurrentUser,
    format: str = Query("txt", pattern="^(json|txt|srt|zip)$"),
):
    """Download results for all succeeded tasks in a batch.

    format=zip returns a zip archive containing all result files.
    Other formats return a concatenated text response (one file per task).
    """
    stmt = (
        select(Task)
        .join(File, Task.file_id == File.file_id)
        .where(Task.task_group_id == group_id, Task.user_id == user_id,
               Task.status == TaskStatus.SUCCEEDED)
        .order_by(Task.created_at.asc())
    )
    tasks = list((await db.execute(stmt)).scalars().all())
    if not tasks:
        raise HTTPException(status_code=404, detail="No succeeded tasks in this group")

    if format == "zip":
        return await _zip_results(tasks)

    if format == "json":
        return await _json_results(tasks)

    parts = []
    for task in tasks:
        content = await read_result(task.task_id, format)
        if content:
            original_name = task.file.original_name if task.file else task.task_id
            parts.append(f"--- {original_name} ---\n{content}\n")

    if not parts:
        raise HTTPException(status_code=404, detail="No result files found")

    media_types = {"txt": "text/plain", "srt": "text/plain"}
    return Response(content="\n".join(parts), media_type=media_types.get(format, "text/plain"))


@router.delete("/{group_id}", status_code=200)
async def delete_task_group(group_id: str, db: DbSession, user_id: CurrentUser):
    """Delete all tasks in a batch (active tasks are protected)."""
    active = {TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING}

    base_where = [Task.task_group_id == group_id, Task.user_id == user_id]

    total_stmt = select(func.count()).select_from(Task).where(*base_where)
    total = (await db.execute(total_stmt)).scalar() or 0
    if total == 0:
        raise HTTPException(status_code=404, detail="Task group not found")

    active_stmt = select(func.count()).select_from(Task).where(
        *base_where, Task.status.in_([s.value for s in active]))
    active_count = (await db.execute(active_stmt)).scalar() or 0

    deletable_where = [*base_where, Task.status.notin_([s.value for s in active])]
    tasks_to_delete = list((await db.execute(select(Task).where(*deletable_where))).scalars().all())

    from app.storage.file_manager import delete_result as _del_result, delete_file as _del_file

    task_ids = {t.task_id for t in tasks_to_delete}
    file_ids = {t.file_id for t in tasks_to_delete if t.file_id}

    still_ref_stmt = (
        select(Task.file_id)
        .where(Task.file_id.in_(file_ids), Task.task_id.notin_(task_ids))
        .distinct()
    )
    still_ref = set((await db.execute(still_ref_stmt)).scalars().all()) if file_ids else set()
    orphaned = file_ids - still_ref

    cleaned_results = 0
    cleaned_files = 0
    deleted_fids: set[str] = set()
    for t in tasks_to_delete:
        if _del_result(t.task_id):
            cleaned_results += 1
        if t.file_id and t.file_id in orphaned and t.file_id not in deleted_fids:
            if _del_file(t.file_id):
                cleaned_files += 1
            deleted_fids.add(t.file_id)

    sub = select(Task.task_id).where(*deletable_where)
    await db.execute(sql_delete(TaskEvent).where(TaskEvent.task_id.in_(sub)))
    await db.execute(sql_delete(Task).where(*deletable_where))
    await db.commit()

    deleted = len(tasks_to_delete)
    logger.info("task_group_deleted", group_id=group_id, deleted=deleted, skipped_active=active_count)
    return {"deleted": deleted, "skipped_active": active_count, "total": total}


async def _group_stats(db, group_id: str, user_id: str) -> dict:
    """Compute batch-level aggregate stats."""
    base = [Task.task_group_id == group_id, Task.user_id == user_id]

    total = (await db.execute(select(func.count()).select_from(Task).where(*base))).scalar() or 0
    if total == 0:
        return {"task_group_id": group_id, "total": 0}

    succeeded = (await db.execute(
        select(func.count()).select_from(Task).where(*base, Task.status == TaskStatus.SUCCEEDED)
    )).scalar() or 0
    failed = (await db.execute(
        select(func.count()).select_from(Task).where(*base, Task.status == TaskStatus.FAILED)
    )).scalar() or 0
    canceled = (await db.execute(
        select(func.count()).select_from(Task).where(*base, Task.status == TaskStatus.CANCELED)
    )).scalar() or 0

    avg_progress = (await db.execute(
        select(func.avg(Task.progress)).where(*base)
    )).scalar() or 0.0

    return {
        "task_group_id": group_id,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "canceled": canceled,
        "in_progress": total - succeeded - failed - canceled,
        "progress": round(float(avg_progress), 4),
        "is_complete": (succeeded + failed + canceled) == total,
    }


async def _json_results(tasks) -> JSONResponse:
    """Return batch results as a valid JSON array."""
    items = []
    for task in tasks:
        content = await read_result(task.task_id, "json")
        if content:
            original_name = task.file.original_name if task.file else task.task_id
            try:
                parsed = _json.loads(content)
            except _json.JSONDecodeError:
                parsed = {"_raw": content}
            items.append({
                "task_id": task.task_id,
                "file_name": original_name,
                "result": parsed,
            })
    if not items:
        raise HTTPException(status_code=404, detail="No result files found")
    return JSONResponse(content=items)


async def _zip_results(tasks) -> StreamingResponse:
    """Create an in-memory zip of all result files."""
    buf = io.BytesIO()
    used_names: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for task in tasks:
            for ext in ("txt", "json", "srt"):
                content = await read_result(task.task_id, ext)
                if content:
                    original = task.file.original_name if task.file else task.task_id
                    stem = original.rsplit(".", 1)[0] if "." in original else original
                    name = f"{stem}.{ext}"
                    if name in used_names:
                        used_names[name] += 1
                        name = f"{stem}_{used_names[name]}.{ext}"
                    else:
                        used_names[name] = 0
                    zf.writestr(name, content)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=batch_results.zip"},
    )
