"""Task management API endpoints."""

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from ulid import ULID

from app.deps import CurrentUser, DbSession
from app.models import Task, TaskStatus
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse
from app.storage.repository import FileRepository, TaskRepository
from app.storage.file_manager import read_result
from app.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


@router.post("", response_model=list[TaskResponse], status_code=201)
async def create_tasks(body: TaskCreateRequest, db: DbSession, user_id: CurrentUser):
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
        logger.info("task_created", task_id=task.task_id, file_id=item.file_id)
    return [TaskResponse.model_validate(t) for t in created_tasks]


@router.get("", response_model=TaskListResponse)
async def list_tasks(db: DbSession, user_id: CurrentUser, status: str | None = Query(None), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    task_repo = TaskRepository(db)
    tasks, total = await task_repo.list_tasks(user_id, status=status, page=page, page_size=page_size)
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
