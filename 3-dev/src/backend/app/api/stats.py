"""System statistics API endpoint."""

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, func

from app.deps import DbSession, CurrentUser
from app.models import Task, TaskStatus, ServerInstance, ServerStatus


router = APIRouter(prefix="/api/v1", tags=["stats"])


class SystemStats(BaseModel):
    server_total: int
    server_online: int
    slots_total: int
    slots_used: int
    queue_depth: int
    tasks_today_completed: int
    tasks_today_failed: int
    success_rate_24h: float
    avg_rtf: float | None


@router.get("/stats", response_model=SystemStats)
async def get_system_stats(db: DbSession, user_id: CurrentUser) -> SystemStats:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    servers = (await db.execute(select(ServerInstance))).scalars().all()
    server_total = len(servers)
    server_online = sum(1 for s in servers if s.status == ServerStatus.ONLINE)
    slots_total = sum(s.max_concurrency for s in servers)

    active_statuses = [TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]
    slots_used_result = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status.in_(active_statuses),
        )
    )
    slots_used = slots_used_result.scalar() or 0

    queue_result = await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status.in_([TaskStatus.PENDING, TaskStatus.PREPROCESSING, TaskStatus.QUEUED]),
        )
    )
    queue_depth = queue_result.scalar() or 0

    today_completed = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status == TaskStatus.SUCCEEDED,
            Task.completed_at >= today_start,
        )
    )).scalar() or 0

    today_failed = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status == TaskStatus.FAILED,
            Task.completed_at >= today_start,
        )
    )).scalar() or 0

    finished_24h = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status.in_([TaskStatus.SUCCEEDED, TaskStatus.FAILED]),
            Task.completed_at >= since_24h,
        )
    )).scalar() or 0
    succeeded_24h = (await db.execute(
        select(func.count()).select_from(Task).where(
            Task.user_id == user_id,
            Task.status == TaskStatus.SUCCEEDED,
            Task.completed_at >= since_24h,
        )
    )).scalar() or 0
    success_rate = (succeeded_24h / finished_24h * 100) if finished_24h > 0 else 100.0

    online_servers = [s for s in servers if s.status == ServerStatus.ONLINE and s.rtf_baseline]
    avg_rtf = (
        sum(s.rtf_baseline for s in online_servers) / len(online_servers)
        if online_servers else None
    )

    return SystemStats(
        server_total=server_total,
        server_online=server_online,
        slots_total=slots_total,
        slots_used=slots_used,
        queue_depth=queue_depth,
        tasks_today_completed=today_completed,
        tasks_today_failed=today_failed,
        success_rate_24h=round(success_rate, 1),
        avg_rtf=round(avg_rtf, 3) if avg_rtf else None,
    )
