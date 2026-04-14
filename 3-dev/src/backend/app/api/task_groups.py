"""Task group (batch) management API endpoints."""

import json as _json
import struct
import time
import zlib
from collections.abc import AsyncIterator

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


@router.delete("/{group_id}")
async def delete_task_group(group_id: str, db: DbSession, user_id: CurrentUser):
    """Delete all tasks in a batch (active tasks are protected).

    Returns 200 on full deletion, 207 Multi-Status when active tasks were skipped.
    Uses atomic DELETE … RETURNING. File cleanup runs after commit.
    """
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

    del_events_sub = select(Task.task_id).where(*deletable_where)
    await db.execute(sql_delete(TaskEvent).where(TaskEvent.task_id.in_(del_events_sub)))

    del_stmt = (
        sql_delete(Task)
        .where(*deletable_where)
        .returning(Task.task_id, Task.file_id)
    )
    deleted_rows = (await db.execute(del_stmt)).all()

    deleted_task_ids = {r.task_id for r in deleted_rows}
    candidate_file_ids = {r.file_id for r in deleted_rows if r.file_id}

    orphaned: set[str] = set()
    if candidate_file_ids:
        still_ref = set(
            (await db.execute(
                select(Task.file_id).where(Task.file_id.in_(candidate_file_ids)).distinct()
            )).scalars().all()
        )
        orphaned = candidate_file_ids - still_ref

    await db.commit()

    from app.storage.file_manager import delete_result as _del_result, delete_file as _del_file
    cleaned_results = 0
    cleaned_files = 0
    deleted_fids: set[str] = set()
    for row in deleted_rows:
        if await _del_result(row.task_id):
            cleaned_results += 1
        if row.file_id and row.file_id in orphaned and row.file_id not in deleted_fids:
            if await _del_file(row.file_id):
                cleaned_files += 1
            deleted_fids.add(row.file_id)

    deleted = len(deleted_task_ids)
    logger.info("task_group_deleted", group_id=group_id, deleted=deleted, skipped_active=active_count)

    body = {"deleted": deleted, "skipped_active": active_count, "total": total,
            "partial": active_count > 0}
    status_code = 207 if active_count > 0 else 200
    return JSONResponse(content=body, status_code=status_code)


async def _group_stats(db, group_id: str, user_id: str) -> dict:
    """Compute batch-level aggregate stats with minimal SQL round-trips."""
    base = [Task.task_group_id == group_id, Task.user_id == user_id]

    stmt = select(
        Task.status,
        func.count().label("cnt"),
        func.avg(Task.progress).label("avg_progress"),
    ).where(*base).group_by(Task.status)

    rows = (await db.execute(stmt)).all()
    if not rows:
        return {"task_group_id": group_id, "total": 0}

    counts: dict[str, int] = {}
    total = 0
    progress_sum = 0.0
    for status, cnt, avg_prog in rows:
        counts[status] = cnt
        total += cnt
        progress_sum += (avg_prog or 0.0) * cnt

    succeeded = counts.get(TaskStatus.SUCCEEDED, 0)
    failed = counts.get(TaskStatus.FAILED, 0)
    canceled = counts.get(TaskStatus.CANCELED, 0)
    avg_progress = progress_sum / total if total else 0.0

    return {
        "task_group_id": group_id,
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "canceled": canceled,
        "in_progress": total - succeeded - failed - canceled,
        "progress": round(avg_progress, 4),
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
    """Stream a zip archive of all result files without buffering the whole zip in memory.

    Uses raw DEFLATE + manual zip structure to yield chunks as each file is compressed.
    Enforces a 500 MB uncompressed-content safety limit.
    """
    MAX_UNCOMPRESSED = 500 * 1024 * 1024

    async def _generate() -> AsyncIterator[bytes]:
        offset = 0
        entries: list[tuple[bytes, int, int, int, int, int]] = []
        total_uncompressed = 0
        used_names: dict[str, int] = {}
        truncated = False

        for task in tasks:
            if truncated:
                break
            for ext in ("txt", "json", "srt"):
                content = await read_result(task.task_id, ext)
                if not content:
                    continue

                raw = content.encode("utf-8") if isinstance(content, str) else content
                total_uncompressed += len(raw)
                if total_uncompressed > MAX_UNCOMPRESSED:
                    truncated = True
                    logger.warning("zip_export_truncated",
                                   limit_mb=MAX_UNCOMPRESSED // 1024 // 1024,
                                   files_included=len(entries))
                    break

                original = task.file.original_name if task.file else task.task_id
                stem = original.rsplit(".", 1)[0] if "." in original else original
                name = f"{stem}.{ext}"
                if name in used_names:
                    used_names[name] += 1
                    name = f"{stem}_{used_names[name]}.{ext}"
                else:
                    used_names[name] = 0
                fname_bytes = name.encode("utf-8")

                crc = zlib.crc32(raw) & 0xFFFFFFFF
                compressed = zlib.compress(raw, 6)[2:-4]  # raw deflate
                comp_size = len(compressed)
                uncomp_size = len(raw)

                mod_time, mod_date = _dos_datetime()

                local_header = struct.pack(
                    "<4sHHHHHIIIHH",
                    b"PK\x03\x04",
                    20, 0, 8,
                    mod_time, mod_date,
                    crc, comp_size, uncomp_size,
                    len(fname_bytes), 0,
                )
                chunk = local_header + fname_bytes + compressed
                entries.append((fname_bytes, offset, crc, comp_size, uncomp_size, mod_time | (mod_date << 16)))
                offset += len(chunk)
                yield chunk

        if truncated:
            notice = b"Export truncated: uncompressed content exceeded 500 MB limit."
            notice_name = b"_TRUNCATED_README.txt"
            crc = zlib.crc32(notice) & 0xFFFFFFFF
            compressed = zlib.compress(notice, 6)[2:-4]
            mod_time, mod_date = _dos_datetime()
            local_header = struct.pack(
                "<4sHHHHHIIIHH",
                b"PK\x03\x04", 20, 0, 8,
                mod_time, mod_date,
                crc, len(compressed), len(notice),
                len(notice_name), 0,
            )
            chunk = local_header + notice_name + compressed
            entries.append((notice_name, offset, crc, len(compressed), len(notice), mod_time | (mod_date << 16)))
            offset += len(chunk)
            yield chunk

        central_offset = offset
        for fname_bytes, local_off, crc, comp_size, uncomp_size, mod_dt in entries:
            mod_time = mod_dt & 0xFFFF
            mod_date = (mod_dt >> 16) & 0xFFFF
            central = struct.pack(
                "<4sHHHHHHIIIHHHHHII",
                b"PK\x01\x02",
                20, 20, 0, 8,
                mod_time, mod_date,
                crc, comp_size, uncomp_size,
                len(fname_bytes), 0, 0, 0, 0, 0x20,
                local_off,
            )
            yield central + fname_bytes

        central_size = sum(46 + len(e[0]) for e in entries)
        eocd = struct.pack(
            "<4sHHHHIIH",
            b"PK\x05\x06",
            0, 0,
            len(entries), len(entries),
            central_size, central_offset,
            0,
        )
        yield eocd

    return StreamingResponse(
        _generate(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=batch_results.zip"},
    )


def _dos_datetime() -> tuple[int, int]:
    """Return (dos_time, dos_date) for the current local time."""
    t = time.localtime()
    dos_time = (t.tm_sec // 2) | (t.tm_min << 5) | (t.tm_hour << 11)
    dos_date = t.tm_mday | (t.tm_mon << 5) | ((t.tm_year - 1980) << 9)
    return dos_time, dos_date
