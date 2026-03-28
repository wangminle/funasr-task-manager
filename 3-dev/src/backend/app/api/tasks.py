"""Task management API endpoints."""

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import delete as sql_delete, func, select
from ulid import ULID

from app.auth.rate_limiter import rate_limiter
from app.deps import CurrentUser, DbSession
from app.models import Task, TaskEvent, TaskStatus
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse
from app.storage.repository import FileRepository, TaskRepository
from app.storage.file_manager import read_result
from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.post("", response_model=list[TaskResponse], status_code=201)
async def create_tasks(body: TaskCreateRequest, db: DbSession, user_id: CurrentUser):
    rate_limiter.check_concurrent_tasks(user_id)
    rate_limiter.check_daily_limit(user_id)
    file_repo = FileRepository(db)
    task_repo = TaskRepository(db)
    task_group_id = str(ULID()) if len(body.items) > 1 else None
    created_tasks: list[Task] = []
    for item in body.items:
        file_record = await file_repo.get_file(item.file_id, user_id)
        if file_record is None:
            raise HTTPException(status_code=404, detail=f"File not found: {item.file_id}")
        task = Task(
            task_id=str(ULID()), user_id=user_id, file_id=item.file_id,
            task_group_id=task_group_id, status=TaskStatus.PENDING,
            language=item.language,
            options_json=json.dumps(item.options) if item.options else None,
            callback_url=body.callback.url if body.callback else None,
            callback_secret=body.callback.secret if body.callback else None,
        )
        task = await task_repo.create_task(task)
        await task_repo.update_task_status(task, TaskStatus.PREPROCESSING)
        created_tasks.append(task)
        rate_limiter.record_task_created(user_id)
        logger.info("task_created", task_id=task.task_id, file_id=item.file_id)
    return [TaskResponse.model_validate(t) for t in created_tasks]


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    db: DbSession, user_id: CurrentUser,
    status: str | None = Query(None),
    search: str | None = Query(None),
    group: str | None = Query(None, description="Filter by task_group_id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    task_repo = TaskRepository(db)
    tasks, total = await task_repo.list_tasks(
        user_id, status=status, search=search, group=group, page=page, page_size=page_size,
    )
    return TaskListResponse(items=[TaskResponse.model_validate(t) for t in tasks], total=total, page=page, page_size=page_size)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, db: DbSession, user_id: CurrentUser):
    task_repo = TaskRepository(db)
    task = await task_repo.get_task(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskResponse.model_validate(task)


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: str, db: DbSession, user_id: CurrentUser):
    task_repo = TaskRepository(db)
    task = await task_repo.get_task(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.can_transition_to(TaskStatus.CANCELED):
        raise HTTPException(status_code=409, detail=f"Cannot cancel task in {task.status} status")
    await task_repo.update_task_status(task, TaskStatus.CANCELED)
    return TaskResponse.model_validate(task)


_ACTIVE_STATUSES = {TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING}


@router.delete("", status_code=200)
async def delete_all_tasks(db: DbSession, user_id: CurrentUser, status: str | None = Query(None)):
    """Delete all tasks (optionally filtered by status). Returns count of deleted tasks.

    Tasks in DISPATCHED or TRANSCRIBING status are always protected — they cannot
    be deleted even when explicitly requested via status filter, because background
    coroutines would still be running and produce orphaned results.
    """
    if status in {s.value for s in _ACTIVE_STATUSES}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot bulk-delete tasks in {status} status. "
                   "Cancel them first via POST /tasks/{{id}}/cancel, then delete.",
        )

    base_where = [Task.user_id == user_id]
    if status:
        base_where.append(Task.status == status)
    else:
        base_where.append(Task.status.notin_([s.value for s in _ACTIVE_STATUSES]))

    stmt_count = select(func.count()).select_from(Task).where(*base_where)
    total = (await db.execute(stmt_count)).scalar() or 0

    skipped = 0
    if not status:
        skip_stmt = select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status.in_([s.value for s in _ACTIVE_STATUSES]),
        )
        skipped = (await db.execute(skip_stmt)).scalar() or 0

    if total == 0:
        return {"deleted": 0, "skipped_active": skipped}

    tasks_to_delete = (await db.execute(select(Task).where(*base_where))).scalars().all()

    task_ids_to_delete = {t.task_id for t in tasks_to_delete}
    candidate_file_ids = {t.file_id for t in tasks_to_delete if t.file_id}

    orphaned_file_ids: set[str] = set()
    if candidate_file_ids:
        still_referenced_stmt = (
            select(Task.file_id)
            .where(Task.file_id.in_(candidate_file_ids), Task.task_id.notin_(task_ids_to_delete))
            .distinct()
        )
        still_referenced = set((await db.execute(still_referenced_stmt)).scalars().all())
        orphaned_file_ids = candidate_file_ids - still_referenced

    from app.storage.file_manager import delete_result as _del_result, delete_file as _del_file
    cleaned_files = 0
    cleaned_results = 0
    deleted_file_ids: set[str] = set()
    for t in tasks_to_delete:
        if _del_result(t.task_id):
            cleaned_results += 1
        if t.file_id and t.file_id in orphaned_file_ids and t.file_id not in deleted_file_ids:
            if _del_file(t.file_id):
                cleaned_files += 1
            deleted_file_ids.add(t.file_id)

    sub = select(Task.task_id).where(*base_where)
    await db.execute(sql_delete(TaskEvent).where(TaskEvent.task_id.in_(sub)))

    stmt_del = sql_delete(Task).where(*base_where)
    await db.execute(stmt_del)
    await db.commit()

    logger.info("tasks_deleted", user_id=user_id, status_filter=status,
                count=total, skipped_active=skipped,
                cleaned_files=cleaned_files, cleaned_results=cleaned_results)
    return {"deleted": total, "skipped_active": skipped}


@router.get("/{task_id}/result")
async def get_task_result(task_id: str, db: DbSession, user_id: CurrentUser, format: str = Query("json", pattern="^(json|txt|srt)$")):
    task_repo = TaskRepository(db)
    task = await task_repo.get_task(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.SUCCEEDED:
        raise HTTPException(status_code=409, detail="Task not yet completed")
    content = await read_result(task_id, format)
    if content is None:
        raise HTTPException(status_code=404, detail="Result file not found")
    media_types = {"json": "application/json", "txt": "text/plain", "srt": "text/plain"}
    return Response(content=content, media_type=media_types.get(format, "text/plain"))
