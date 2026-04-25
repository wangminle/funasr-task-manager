"""Repository pattern for database operations."""

from datetime import datetime, timezone

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import File, Task, TaskEvent, ServerInstance, TaskStatus, TaskSegment, SegmentStatus
from app.observability.logging import get_logger

logger = get_logger(__name__)


class TaskRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_task(self, task: Task) -> Task:
        self._session.add(task)
        await self._session.flush()
        return task

    async def get_task(self, task_id: str, user_id: str | None = None) -> Task | None:
        stmt = select(Task).where(Task.task_id == task_id)
        if user_id:
            stmt = stmt.where(Task.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_tasks(self, user_id: str, status: str | None = None, search: str | None = None, group: str | None = None, page: int = 1, page_size: int = 20) -> tuple[list[Task], int]:
        base = select(Task).where(Task.user_id == user_id)
        if status:
            base = base.where(Task.status == status)
        if group:
            base = base.where(Task.task_group_id == group)
        if search:
            pattern = f"%{search}%"
            base = base.join(File, Task.file_id == File.file_id).where(
                (Task.task_id.ilike(pattern)) | (File.original_name.ilike(pattern))
            )
        count_stmt = select(func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_stmt)).scalar() or 0
        stmt = base.order_by(Task.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        result = await self._session.execute(stmt)
        tasks = list(result.scalars().all())
        return tasks, total

    async def update_task_status(self, task: Task, new_status: TaskStatus, payload: str | None = None) -> TaskEvent:
        from ulid import ULID
        from_status = task.transition_to(new_status)
        event = TaskEvent(
            event_id=str(ULID()), task_id=task.task_id,
            from_status=from_status, to_status=new_status.value, payload_json=payload,
        )
        self._session.add(event)
        await self._session.flush()
        logger.info("task_status_changed", task_id=task.task_id, from_status=from_status, to_status=new_status.value)
        return event

    async def get_pending_tasks(self, limit: int = 10) -> list[Task]:
        stmt = select(Task).where(Task.status == TaskStatus.QUEUED).order_by(Task.created_at.asc()).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class FileRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_file(self, file_id: str, user_id: str | None = None) -> File | None:
        stmt = select(File).where(File.file_id == file_id)
        if user_id:
            stmt = stmt.where(File.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()


class ServerRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_server(self, server_id: str) -> ServerInstance | None:
        result = await self._session.execute(select(ServerInstance).where(ServerInstance.server_id == server_id))
        return result.scalar_one_or_none()

    async def list_online_servers(self) -> list[ServerInstance]:
        result = await self._session.execute(select(ServerInstance).where(ServerInstance.status == "ONLINE"))
        return list(result.scalars().all())

    async def list_all_servers(self) -> list[ServerInstance]:
        result = await self._session.execute(select(ServerInstance))
        return list(result.scalars().all())

    async def register_server(self, server: ServerInstance) -> ServerInstance:
        self._session.add(server)
        await self._session.flush()
        return server

    async def delete_server(self, server_id: str) -> bool:
        server = await self.get_server(server_id)
        if server is None:
            return False
        await self._session.delete(server)
        await self._session.flush()
        return True

    async def get_all_servers_brief(self) -> list[dict]:
        """Return lightweight dicts for the heartbeat service."""
        servers = await self.list_all_servers()
        return [
            {
                "server_id": s.server_id,
                "host": s.host,
                "port": s.port,
                "status": s.status,
                "last_heartbeat": s.last_heartbeat,
            }
            for s in servers
        ]

    async def update_server_status(
        self, server_id: str, new_status: str, last_heartbeat: datetime | None,
    ) -> None:
        server = await self.get_server(server_id)
        if server is None:
            return
        server.status = new_status
        if last_heartbeat is not None:
            server.last_heartbeat = last_heartbeat
        await self._session.flush()


class SegmentRepository:
    """CRUD operations for TaskSegment (VAD parallel transcription work units)."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_segments(self, segments: list[TaskSegment]) -> list[TaskSegment]:
        self._session.add_all(segments)
        await self._session.flush()
        return segments

    async def get_segment(self, segment_id: str) -> TaskSegment | None:
        stmt = select(TaskSegment).where(TaskSegment.segment_id == segment_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_segments_by_task(self, task_id: str) -> list[TaskSegment]:
        stmt = (
            select(TaskSegment)
            .where(TaskSegment.task_id == task_id)
            .order_by(TaskSegment.segment_index.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_status(self, task_id: str) -> dict[str, int]:
        """Return {status: count} for all segments of a parent task."""
        stmt = (
            select(TaskSegment.status, func.count())
            .where(TaskSegment.task_id == task_id)
            .group_by(TaskSegment.status)
        )
        rows = (await self._session.execute(stmt)).all()
        return {status: count for status, count in rows}

    async def count_active_segments(self, task_id: str) -> int:
        """Count segments currently DISPATCHED or TRANSCRIBING for a parent task."""
        stmt = (
            select(func.count())
            .where(
                TaskSegment.task_id == task_id,
                TaskSegment.status.in_([
                    SegmentStatus.DISPATCHED,
                    SegmentStatus.TRANSCRIBING,
                ]),
            )
        )
        return (await self._session.execute(stmt)).scalar() or 0

    async def get_pending_segments(
        self, task_id: str, limit: int = 10,
    ) -> list[TaskSegment]:
        stmt = (
            select(TaskSegment)
            .where(
                TaskSegment.task_id == task_id,
                TaskSegment.status == SegmentStatus.PENDING,
            )
            .order_by(TaskSegment.segment_index.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_segment_status(
        self,
        segment: TaskSegment,
        new_status: SegmentStatus,
        *,
        server_id: str | None = None,
        error_message: str | None = None,
        raw_result_json: str | None = None,
    ) -> None:
        segment.status = new_status.value
        now = datetime.now(timezone.utc)
        if new_status == SegmentStatus.DISPATCHED and server_id:
            segment.assigned_server_id = server_id
        if new_status == SegmentStatus.TRANSCRIBING:
            segment.started_at = now
        if new_status == SegmentStatus.SUCCEEDED:
            segment.completed_at = now
            if raw_result_json is not None:
                segment.raw_result_json = raw_result_json
        if new_status == SegmentStatus.FAILED:
            segment.completed_at = now
            if error_message is not None:
                segment.error_message = error_message[:2000]
        await self._session.flush()

    async def increment_retry(self, segment: TaskSegment) -> None:
        prev_error = segment.error_message or ""
        segment.retry_count += 1
        segment.status = SegmentStatus.PENDING.value
        segment.assigned_server_id = None
        if prev_error:
            history = f"[retry#{segment.retry_count - 1}] {prev_error}"
            segment.error_message = history[-2000:]
        segment.started_at = None
        segment.completed_at = None
        await self._session.flush()

    async def sum_completed_duration_ms(self, task_id: str) -> int:
        """Sum of keep_duration for all SUCCEEDED segments — used for progress calculation."""
        stmt = (
            select(func.coalesce(
                func.sum(TaskSegment.keep_end_ms - TaskSegment.keep_start_ms), 0,
            ))
            .where(
                TaskSegment.task_id == task_id,
                TaskSegment.status == SegmentStatus.SUCCEEDED,
            )
        )
        return (await self._session.execute(stmt)).scalar() or 0

    async def total_keep_duration_ms(self, task_id: str) -> int:
        """Sum of keep_duration for ALL segments — the denominator for progress."""
        stmt = (
            select(func.coalesce(
                func.sum(TaskSegment.keep_end_ms - TaskSegment.keep_start_ms), 0,
            ))
            .where(TaskSegment.task_id == task_id)
        )
        return (await self._session.execute(stmt)).scalar() or 0

    async def list_segments_by_status(
        self, task_id: str, status: SegmentStatus,
    ) -> list[TaskSegment]:
        stmt = (
            select(TaskSegment)
            .where(TaskSegment.task_id == task_id, TaskSegment.status == status)
            .order_by(TaskSegment.segment_index.asc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def delete_segments_by_task(self, task_id: str) -> int:
        """Delete all segments for a task. Returns count deleted."""
        stmt = select(TaskSegment).where(TaskSegment.task_id == task_id)
        result = await self._session.execute(stmt)
        segments = list(result.scalars().all())
        for seg in segments:
            await self._session.delete(seg)
        await self._session.flush()
        return len(segments)
