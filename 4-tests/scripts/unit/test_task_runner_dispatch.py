"""Task runner dispatch behavior tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import File, ServerInstance, Task, TaskStatus
from app.services.task_runner import BackgroundTaskRunner


def _server(server_id: str, max_concurrency: int) -> ServerInstance:
    return ServerInstance(
        server_id=server_id,
        host="127.0.0.1",
        port=10095,
        protocol_version="v2_new",
        max_concurrency=max_concurrency,
        status="ONLINE",
        rtf_baseline=0.2,
        penalty_factor=0.1,
    )


def _file(file_id: str, duration_sec: float) -> File:
    return File(
        file_id=file_id,
        user_id="test-user",
        original_name=f"{file_id}.wav",
        media_type="audio",
        mime="audio/wav",
        duration_sec=duration_sec,
        size_bytes=1024,
        storage_path=f"/tmp/{file_id}.wav",
        status="UPLOADED",
    )


def _task(task_id: str, file_id: str, status: TaskStatus) -> Task:
    return Task(
        task_id=task_id,
        user_id="test-user",
        file_id=file_id,
        task_group_id="group-01",
        status=status,
        progress=0.15,
        language="zh",
    )


@pytest.mark.unit
class TestTaskRunnerDispatch:
    async def test_dispatches_only_immediate_wave(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr(
            "app.services.task_runner.breaker_registry",
            SimpleNamespace(get=lambda _server_id: SimpleNamespace(
                allow_request=lambda: True,
                record_failure=lambda: None,
                record_success=lambda: None,
            )),
        )

        async with session_factory() as session:
            session.add_all([
                _server("srv-1", 2),
                _server("srv-2", 2),
            ])
            for index in range(5):
                file_id = f"file-{index}"
                session.add(_file(file_id, duration_sec=120 + index * 30))
                session.add(_task(f"task-{index}", file_id, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started_task_ids: list[str] = []

        async def _fake_execute_task(task_id: str):
            started_task_ids.append(task_id)

        monkeypatch.setattr(runner, "_execute_task", _fake_execute_task)

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            tasks = list((await session.execute(select(Task).order_by(Task.task_id.asc()))).scalars().all())

        dispatched = [task for task in tasks if task.status == TaskStatus.DISPATCHED]
        queued = [task for task in tasks if task.status == TaskStatus.QUEUED]

        assert len(dispatched) == 4
        assert len(queued) == 1
        assert all(task.assigned_server_id for task in dispatched)
        assert queued[0].assigned_server_id is None
        assert len(started_task_ids) == 4

    async def test_completion_requests_next_dispatch(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("file-complete", duration_sec=180))
            session.add(_task("task-complete", "file-complete", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        runner._dispatch_event.clear()

        await runner._mark_task_succeeded("task-complete")

        assert runner._dispatch_event.is_set()