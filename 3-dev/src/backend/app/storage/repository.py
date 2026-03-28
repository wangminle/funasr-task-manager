"""Repository pattern for database operations."""

from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import File, Task, TaskEvent, ServerInstance, TaskStatus
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
