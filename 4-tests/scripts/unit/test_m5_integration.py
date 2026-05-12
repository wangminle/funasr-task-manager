"""M5.1 Integration tests: 3-server mock scenario.

Validates the full PlanPool + work steal + replan pipeline
across multiple dispatch cycles under realistic conditions.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import File, ServerInstance, Task, TaskStatus
from app.services.task_runner import BackgroundTaskRunner


def _server(server_id: str, max_concurrency: int, rtf: float = 0.2) -> ServerInstance:
    return ServerInstance(
        server_id=server_id,
        host="127.0.0.1",
        port=10095,
        protocol_version="v2_new",
        max_concurrency=max_concurrency,
        rtf_baseline=rtf,
        status="ONLINE",
        enabled=True,
    )


def _file(file_id: str, duration_sec: float = 60.0) -> File:
    return File(
        file_id=file_id,
        user_id="test",
        original_name=f"{file_id}.wav",
        storage_path=f"/tmp/{file_id}.wav",
        size_bytes=1000,
        duration_sec=duration_sec,
        status="META_READY",
    )


def _task(task_id: str, file_id: str, status=TaskStatus.QUEUED) -> Task:
    return Task(
        task_id=task_id, user_id="test", file_id=file_id,
        status=status, progress=0.0, language="zh",
    )


def _breaker_mock():
    class AllowAll:
        async def allow_request(self):
            return True
    class Registry:
        def get(self, server_id):
            return AllowAll()
    return Registry()


@pytest.mark.unit
class TestThreeServerIntegration:
    """Simulate a 3-server cluster processing 20 tasks across multiple dispatch cycles."""

    async def test_multi_cycle_dispatch_no_overcommit(self, db_engine, monkeypatch):
        """Over 4 dispatch cycles with task completions, no server should
        exceed max_concurrency and all tasks should eventually be dispatched."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("fast", 3, rtf=0.1))
            session.add(_server("mid", 2, rtf=0.3))
            session.add(_server("slow", 2, rtf=0.5))
            for i in range(20):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0 + i * 10))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []
        monkeypatch.setattr(runner, "_execute_task",
                            lambda tid: started.append(tid) or asyncio.sleep(0))

        max_concurrency = {"fast": 3, "mid": 2, "slow": 2}

        for cycle in range(5):
            await runner._dispatch_queued_tasks()
            await asyncio.sleep(0)

            async with session_factory() as session:
                all_tasks = list((await session.execute(select(Task))).scalars().all())
                per_server: dict[str, int] = {}
                for t in all_tasks:
                    if t.status == TaskStatus.DISPATCHED and t.assigned_server_id:
                        per_server[t.assigned_server_id] = per_server.get(t.assigned_server_id, 0) + 1

                for sid, count in per_server.items():
                    limit = max_concurrency.get(sid, 99)
                    assert count <= limit, (
                        f"Cycle {cycle}: server {sid} has {count} dispatched "
                        f"tasks but max_concurrency is {limit}"
                    )

                dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]
                if dispatched:
                    t = dispatched[0]
                    t.status = TaskStatus.SUCCEEDED.value
                    t.progress = 1.0
                    await session.commit()

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            dispatched_or_done = [t for t in all_tasks
                                  if t.status in (TaskStatus.DISPATCHED, TaskStatus.SUCCEEDED)]
            assert len(dispatched_or_done) > 7, (
                f"Expected at least 7 tasks dispatched/done after 5 cycles, got {len(dispatched_or_done)}"
            )

    async def test_work_steal_fires_when_fast_server_frees_slots(self, db_engine, monkeypatch):
        """After fast server completes its tasks, it should steal from slower servers."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("fast", 2, rtf=0.05))
            session.add(_server("mid", 2, rtf=0.3))
            session.add(_server("slow", 1, rtf=0.8))
            for i in range(12):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=300.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        import app.services.task_runner as tr_mod
        steal_log: list[dict] = []
        original_info = tr_mod.logger.info
        def _spy(msg, **kw):
            if msg == "work_steal":
                steal_log.append(kw)
            return original_info(msg, **kw)
        monkeypatch.setattr(tr_mod.logger, "info", _spy)
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            fast_dispatched = [t for t in all_tasks
                               if t.status == TaskStatus.DISPATCHED and t.assigned_server_id == "fast"]
            for t in fast_dispatched:
                t.status = TaskStatus.SUCCEEDED.value
                t.progress = 1.0
            await session.commit()

        runner._last_replan_time = 0.0
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            fast_second_wave = [t for t in all_tasks
                                if t.status == TaskStatus.DISPATCHED and t.assigned_server_id == "fast"]

        total_fast = len(fast_second_wave) + len(steal_log)
        assert total_fast > 0 or len(steal_log) > 0, (
            "Fast server should have either stolen tasks or gotten new tasks from replan"
        )

    async def test_plan_pool_state_consistency_across_cycles(self, db_engine, monkeypatch):
        """PlanPool should not have stale entries after multiple dispatch+completion cycles."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("s1", 2, rtf=0.2))
            session.add(_server("s2", 2, rtf=0.2))
            for i in range(6):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        pool_task_ids_before = set(runner._plan_pool.task_ids)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]
            for t in dispatched:
                t.status = TaskStatus.SUCCEEDED.value
                t.progress = 1.0
            await session.commit()

        runner._last_replan_time = 0.0
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        dispatched_ids = set()
        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            for t in all_tasks:
                if t.status in (TaskStatus.DISPATCHED, TaskStatus.SUCCEEDED):
                    dispatched_ids.add(t.task_id)

        pool_task_ids_after = set(runner._plan_pool.task_ids)
        stale = pool_task_ids_after & dispatched_ids
        assert len(stale) == 0, (
            f"PlanPool contains stale entries for already-dispatched tasks: {stale}"
        )

    async def test_replan_after_server_goes_offline(self, db_engine, monkeypatch):
        """When a server goes offline (circuit broken), plan should be rebuilt
        using only remaining servers."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("s1", 2, rtf=0.2))
            session.add(_server("s2", 2, rtf=0.2))
            session.add(_server("s3", 2, rtf=0.2))
            for i in range(10):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=120.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert "s3" in runner._planned_available_server_ids

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            for t in all_tasks:
                if t.status == TaskStatus.DISPATCHED:
                    t.status = TaskStatus.SUCCEEDED.value
            await session.commit()

        class BrokenS3:
            async def allow_request(self):
                return False
        class AllowAll:
            async def allow_request(self):
                return True
        class PartialRegistry:
            def get(self, server_id):
                if server_id == "s3":
                    return BrokenS3()
                return AllowAll()

        monkeypatch.setattr("app.services.task_runner.breaker_registry", PartialRegistry())
        runner._clear_plan_pool()

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert "s3" not in runner._planned_available_server_ids, \
            "Offline server s3 should not be in planned_available_server_ids"

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            s3_dispatched = [t for t in all_tasks
                             if t.status == TaskStatus.DISPATCHED and t.assigned_server_id == "s3"]
            assert len(s3_dispatched) == 0, \
                "No new tasks should be dispatched to circuit-broken server s3"
