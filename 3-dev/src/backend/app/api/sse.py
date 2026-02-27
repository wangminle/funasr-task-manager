"""SSE (Server-Sent Events) endpoint for real-time task progress."""

import asyncio
import json
from datetime import datetime, timezone
from collections.abc import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.deps import CurrentUser, DbSession
from app.models import TaskStatus
from app.storage.repository import TaskRepository
from app.services.progress import calculate_progress, calculate_eta, format_progress_message
from app.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/tasks", tags=["progress"])

SSE_POLL_INTERVAL = 1.0
SSE_KEEPALIVE_INTERVAL = 15.0


def _format_sse(event_type: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


async def _progress_stream(
    task_id: str,
    user_id: str,
    db_session_factory,
) -> AsyncGenerator[str, None]:
    """Generate SSE events for a task's progress."""
    last_status = None
    last_progress = -1.0
    keepalive_counter = 0

    while True:
        try:
            async with db_session_factory() as session:
                repo = TaskRepository(session)
                task = await repo.get_task(task_id, user_id)

                if task is None:
                    yield _format_sse("error", {"message": "Task not found"})
                    return

                file_duration = None
                if task.file:
                    file_duration = task.file.duration_sec

                progress = calculate_progress(
                    task.status,
                    started_at=task.started_at,
                    duration_sec=file_duration,
                )
                eta = calculate_eta(
                    task.status,
                    started_at=task.started_at,
                    duration_sec=file_duration,
                )
                message = format_progress_message(task.status, progress)

                status_changed = task.status != last_status
                progress_changed = abs(progress - last_progress) > 0.01

                if status_changed or progress_changed:
                    event_data = {
                        "task_id": task_id,
                        "event_type": "status_change" if status_changed else "progress_update",
                        "status": task.status,
                        "progress": round(progress, 4),
                        "eta_seconds": eta,
                        "message": message,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

                    if task.status == TaskStatus.SUCCEEDED:
                        event_data["result_available"] = task.result_path is not None

                    if task.status == TaskStatus.FAILED:
                        event_data["error_code"] = task.error_code
                        event_data["error_message"] = task.error_message

                    yield _format_sse(event_data["event_type"], event_data)
                    last_status = task.status
                    last_progress = progress
                    keepalive_counter = 0

                    terminal = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}
                    if TaskStatus(task.status) in terminal:
                        yield _format_sse("complete", {
                            "task_id": task_id,
                            "final_status": task.status,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        return
                else:
                    keepalive_counter += 1
                    if keepalive_counter * SSE_POLL_INTERVAL >= SSE_KEEPALIVE_INTERVAL:
                        yield f": keepalive\n\n"
                        keepalive_counter = 0

        except Exception as e:
            logger.error("sse_stream_error", task_id=task_id, error=str(e))
            yield _format_sse("error", {"message": "Internal error", "detail": str(e)})
            return

        await asyncio.sleep(SSE_POLL_INTERVAL)


@router.get("/{task_id}/progress")
async def task_progress_sse(task_id: str, db: DbSession, user_id: CurrentUser):
    """SSE endpoint for real-time task progress updates."""
    repo = TaskRepository(db)
    task = await repo.get_task(task_id, user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.storage.database import async_session_factory

    return StreamingResponse(
        _progress_stream(task_id, user_id, async_session_factory),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
