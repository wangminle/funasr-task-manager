"""Task runner dispatch behavior tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import File, ServerInstance, Task, TaskStatus
from app.models.task_segment import SegmentStatus, TaskSegment
from app.services.task_runner import BackgroundTaskRunner


def _server(server_id: str, max_concurrency: int, rtf: float = 0.2) -> ServerInstance:
    return ServerInstance(
        server_id=server_id,
        host="127.0.0.1",
        port=10095,
        protocol_version="v2_new",
        max_concurrency=max_concurrency,
        status="ONLINE",
        rtf_baseline=rtf,
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


def _breaker_mock():
    async def _allow():
        return True

    async def _noop():
        return None

    return SimpleNamespace(get=lambda _server_id: SimpleNamespace(
        allow_request=_allow,
        record_failure=_noop,
        record_success=_noop,
    ))


def _breaker_mock_with_broken(broken_server_ids: set[str]):
    """Circuit breaker mock where specified servers are tripped."""
    async def _noop():
        return None

    def _get(server_id):
        async def _allow(sid=server_id):
            return sid not in broken_server_ids
        return SimpleNamespace(
            allow_request=_allow,
            record_failure=_noop,
            record_success=_noop,
        )
    return SimpleNamespace(get=_get)


@pytest.mark.unit
class TestTaskRunnerDispatch:
    async def test_dispatches_only_immediate_wave(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

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

    async def test_late_completion_from_queued_recovers_to_succeeded(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("file-late", duration_sec=180))
            session.add(_task("task-late", "file-late", TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        await runner._mark_task_succeeded("task-late")

        async with session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.task_id == "task-late")
            )).scalar_one()

        assert task.status == TaskStatus.SUCCEEDED.value
        assert task.progress == 1.0
        assert task.result_path == "task-late"


@pytest.mark.unit
class TestPlanPoolDispatch:
    """Verify that PlanPool caches the plan and avoids redundant re-planning."""

    async def test_plan_pool_populated_after_first_dispatch(self, db_engine, monkeypatch):
        """First dispatch should populate PlanPool for deferred tasks."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 2))
            for i in range(6):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert runner._plan_pool, "PlanPool should hold deferred tasks"
        assert len(runner._plan_pool) > 0, "Some tasks should remain in PlanPool"

    async def test_second_dispatch_pops_from_pool(self, db_engine, monkeypatch):
        """After a task completes, second dispatch should pop from PlanPool."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1))
            for i in range(3):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0 * (3 - i)))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []
        monkeypatch.setattr(runner, "_execute_task", lambda tid: started.append(tid) or asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)
        first_wave = list(started)
        assert len(first_wave) == 1

        deferred_before = len(runner._plan_pool)
        assert deferred_before == 2

        async with session_factory() as session:
            dispatched_task = (await session.execute(
                select(Task).where(Task.task_id == first_wave[0])
            )).scalar_one()
            dispatched_task.status = TaskStatus.SUCCEEDED
            await session.commit()

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert len(started) == 2
        deferred_after = len(runner._plan_pool)
        assert deferred_after == deferred_before - 1


@pytest.mark.unit
class TestWorkStealing:
    """Verify work stealing dispatches tasks to idle servers."""

    async def test_idle_server_steals_from_busy_queue(self, db_engine, monkeypatch):
        """A fast server that finishes early should steal tasks from a slower server's queue."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("fast", 2, rtf=0.1))
            session.add(_server("slow", 2, rtf=0.5))
            for i in range(8):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=120.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []
        monkeypatch.setattr(runner, "_execute_task", lambda tid: started.append(tid) or asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)
        first_wave_count = len(started)
        assert first_wave_count >= 3, f"First wave should dispatch at least 3, got {first_wave_count}"

        async with session_factory() as session:
            for tid in started[:]:
                t = (await session.execute(select(Task).where(Task.task_id == tid))).scalar_one()
                if t.assigned_server_id == "fast":
                    t.status = TaskStatus.SUCCEEDED
            await session.commit()

        started.clear()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]
            fast_dispatched = [t for t in dispatched if t.assigned_server_id == "fast"]

        assert len(fast_dispatched) > 0, "Fast server should have stolen tasks after becoming idle"

    async def test_no_steal_when_server_has_own_queue(self, db_engine, monkeypatch):
        """A server that still has planned queue work should NOT steal from others."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("a", 1, rtf=0.2))
            session.add(_server("b", 1, rtf=0.2))
            for i in range(4):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        steal_log: list[str] = []
        orig_info = __import__("app.observability.logging", fromlist=["get_logger"]).get_logger(__name__).info

        import app.services.task_runner as tr_mod
        original_logger_info = tr_mod.logger.info

        def _spy_info(msg, **kw):
            if msg == "work_steal":
                steal_log.append(kw.get("task_id", ""))
            return original_logger_info(msg, **kw)

        monkeypatch.setattr(tr_mod.logger, "info", _spy_info)
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert len(steal_log) == 0, "No stealing should happen when both servers have planned work"


@pytest.mark.unit
class TestCircuitBreakerPlanInvalidation:
    """Verify that cached plans are invalidated when available server set changes."""

    async def test_circuit_broken_server_excluded_from_dispatch(self, db_engine, monkeypatch):
        """After a server is circuit-broken, cached plan must be invalidated
        and no tasks should be dispatched to the broken server."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 2, rtf=0.2))
            session.add(_server("srv-2", 2, rtf=0.2))
            for i in range(6):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []
        monkeypatch.setattr(runner, "_execute_task", lambda tid: started.append(tid) or asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)
        first_wave = list(started)

        async with session_factory() as session:
            for tid in first_wave:
                t = (await session.execute(select(Task).where(Task.task_id == tid))).scalar_one()
                t.status = TaskStatus.SUCCEEDED
            await session.commit()

        monkeypatch.setattr("app.services.task_runner.breaker_registry",
                            _breaker_mock_with_broken({"srv-1"}))

        started.clear()
        runner._clear_plan_pool()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            newly_dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]

        for t in newly_dispatched:
            assert t.assigned_server_id != "srv-1", \
                f"Task {t.task_id} dispatched to circuit-broken server srv-1"

    async def test_plan_invalidated_on_server_set_change(self, db_engine, monkeypatch):
        """When available server set changes, PlanPool should be rebuilt."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1, rtf=0.2))
            session.add(_server("srv-2", 1, rtf=0.2))
            for i in range(4):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert runner._planned_available_server_ids == frozenset({"srv-1", "srv-2"})

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            for t in all_tasks:
                if t.status == TaskStatus.DISPATCHED:
                    t.status = TaskStatus.SUCCEEDED
            await session.commit()

        monkeypatch.setattr("app.services.task_runner.breaker_registry",
                            _breaker_mock_with_broken({"srv-2"}))

        runner._clear_plan_pool()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert runner._planned_available_server_ids == frozenset({"srv-1"})

    async def test_free_slots_only_from_available_servers(self, db_engine, monkeypatch):
        """free_slots must only count servers passing circuit breaker check."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry",
                            _breaker_mock_with_broken({"broken-srv"}))

        async with session_factory() as session:
            session.add(_server("broken-srv", 4, rtf=0.1))
            session.add(_server("healthy-srv", 2, rtf=0.3))
            for i in range(3):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=60.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []
        monkeypatch.setattr(runner, "_execute_task", lambda tid: started.append(tid) or asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]

        for t in dispatched:
            assert t.assigned_server_id == "healthy-srv", \
                f"Task {t.task_id} assigned to broken server"


@pytest.mark.unit
class TestPerSlotWorkStealing:
    """Verify per-slot work stealing: idle slots on a multi-concurrency server
    should be able to steal even if other slots on the same server have queue work."""

    async def test_idle_slot_steals_while_sibling_has_queue(self, db_engine, monkeypatch):
        """A server with some idle slots and some busy slots should steal
        with the idle slots, not wait for all own queues to drain."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("fast", 4, rtf=0.1))
            session.add(_server("slow", 1, rtf=0.5))
            for i in range(6):
                fid = f"f-{i}"
                session.add(_file(fid, duration_sec=120.0))
                session.add(_task(f"t-{i}", fid, TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []

        import app.services.task_runner as tr_mod
        original_logger_info = tr_mod.logger.info
        steal_log: list[str] = []

        def _spy_info(msg, **kw):
            if msg == "work_steal":
                steal_log.append(kw.get("to_server", ""))
            return original_logger_info(msg, **kw)

        monkeypatch.setattr(tr_mod.logger, "info", _spy_info)
        monkeypatch.setattr(runner, "_execute_task", lambda tid: started.append(tid) or asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]
            fast_dispatched = [t for t in dispatched if t.assigned_server_id == "fast"]

        assert len(fast_dispatched) >= 4, (
            f"Fast server with 4 slots should dispatch at least 4 tasks, got {len(fast_dispatched)}"
        )


@pytest.mark.unit
class TestPlanInvalidationOnCompletion:
    """Verify PlanPool plan invalidation on task completion (conservative clearing)."""

    async def test_plan_preserved_when_pool_still_has_items_on_success(
        self, db_engine, monkeypatch,
    ):
        """Do not wipe the global plan while items remain in PlanPool."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("f-done", duration_sec=60.0))
            session.add(_task("t-done", "f-done", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        from app.services.scheduler import ScheduleDecision
        runner._plan_pool.merge([
            ScheduleDecision("t-fake", "srv-1", 0, 10.0, 5.0, 15.0, 2, 30.0),
        ])
        runner._planned_available_server_ids = frozenset({"srv-1"})

        await runner._mark_task_succeeded("t-done")

        assert runner._plan_pool.contains("t-fake")
        assert runner._planned_available_server_ids == frozenset({"srv-1"})
        assert runner._dispatch_event.is_set()

    async def test_plan_preserved_when_pool_still_has_items_on_failure(
        self, db_engine, monkeypatch,
    ):
        """Same as success path: pending PlanPool work keeps the plan intact."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("f-fail", duration_sec=60.0))
            session.add(_task("t-fail", "f-fail", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        from app.services.scheduler import ScheduleDecision
        runner._plan_pool.merge([
            ScheduleDecision("t-fake2", "srv-1", 0, 10.0, 5.0, 15.0, 2, 30.0),
        ])
        runner._planned_available_server_ids = frozenset({"srv-1"})

        await runner._mark_task_failed("t-fail", "some error")

        assert runner._plan_pool.contains("t-fake2")
        assert runner._planned_available_server_ids == frozenset({"srv-1"})
        assert runner._dispatch_event.is_set()

    async def test_plan_cleared_on_task_success_when_pool_empty(
        self, db_engine, monkeypatch,
    ):
        """When PlanPool is empty, completion clears the plan for next replan."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("f-done2", duration_sec=60.0))
            session.add(_task("t-done2", "f-done2", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        runner._planned_available_server_ids = frozenset({"srv-1"})

        await runner._mark_task_succeeded("t-done2")

        assert not runner._plan_pool
        assert runner._planned_available_server_ids == frozenset()
        assert runner._dispatch_event.is_set()


@pytest.mark.unit
class TestStealImprovementCalculation:
    """Verify work stealing uses remaining source queue time, not stale estimated_finish."""

    def test_find_steal_candidate_uses_remaining_time(self):
        """Improvement should be computed as source_remaining - est_stolen,
        not decision.estimated_finish - est_stolen."""
        from app.services.scheduler import ScheduleDecision, ServerProfile
        from app.services.scheduler import scheduler as global_scheduler

        runner = BackgroundTaskRunner()

        runner._plan_pool.merge([
            ScheduleDecision("t-ahead", "slow", 0, 0.0, 30.0, 30.0, 1, 100.0),
            ScheduleDecision("t-target", "slow", 0, 30.0, 25.0, 55.0, 2, 80.0),
        ])

        idle_profile = ServerProfile(
            server_id="fast", host="h", port=1,
            max_concurrency=4, rtf_baseline=0.1, penalty_factor=0.1,
        )
        profile_map = {
            "fast": idle_profile,
            "slow": ServerProfile(
                server_id="slow", host="h", port=1,
                max_concurrency=2, rtf_baseline=0.5, penalty_factor=0.1,
            ),
        }

        class FakeTask:
            def __init__(self, tid):
                self.task_id = tid
        task_map = {"t-ahead": FakeTask("t-ahead"), "t-target": FakeTask("t-target")}

        result = runner._find_steal_candidate(idle_profile, profile_map, task_map, set())
        assert result is not None
        decision, source_server, est_stolen, source_remaining_actual, estimated_gain = result
        assert decision.task_id == "t-target"

        est_on_idle = global_scheduler.estimate_processing_time(80.0, idle_profile)
        source_remaining = 30.0 + 25.0
        expected_improvement = source_remaining - est_on_idle
        assert expected_improvement > 0
        assert source_remaining_actual == pytest.approx(source_remaining)
        assert estimated_gain == pytest.approx(expected_improvement)

    def test_no_steal_when_source_faster_than_idle(self):
        """Should NOT steal when the source server processes faster than idle server."""
        from app.services.scheduler import ScheduleDecision, ServerProfile

        runner = BackgroundTaskRunner()

        runner._plan_pool.merge([
            ScheduleDecision("t-only", "fast", 0, 0.0, 8.0, 8.0, 1, 10.0),
        ])

        idle_profile = ServerProfile(
            server_id="slow-idle", host="h", port=1,
            max_concurrency=2, rtf_baseline=1.0, penalty_factor=0.1,
        )
        profile_map = {
            "slow-idle": idle_profile,
            "fast": ServerProfile(
                server_id="fast", host="h", port=1,
                max_concurrency=2, rtf_baseline=0.05, penalty_factor=0.1,
            ),
        }

        class FakeTask:
            def __init__(self, tid):
                self.task_id = tid
        task_map = {"t-only": FakeTask("t-only")}

        result = runner._find_steal_candidate(idle_profile, profile_map, task_map, set())
        assert result is None, "Should not steal when idle server is slower than source"

    def test_no_steal_when_estimated_gain_is_too_small(self):
        """Small positive improvements should not churn PlanPool."""
        from app.services.scheduler import ScheduleDecision, ServerProfile

        runner = BackgroundTaskRunner()
        runner._plan_pool.merge([
            ScheduleDecision("candidate", "source", 0, 0.0, 14.0, 14.0, 1, 40.0),
        ])

        idle_profile = ServerProfile(
            server_id="idle", host="h", port=1,
            max_concurrency=1, rtf_baseline=0.2, penalty_factor=0.1,
        )
        profile_map = {
            "idle": idle_profile,
            "source": ServerProfile(
                server_id="source", host="h", port=1,
                max_concurrency=1, rtf_baseline=0.25, penalty_factor=0.1,
            ),
        }

        class FakeTask:
            def __init__(self, tid):
                self.task_id = tid
        task_map = {"candidate": FakeTask("candidate")}

        result = runner._find_steal_candidate(idle_profile, profile_map, task_map, set())
        assert result is None, "Should not steal when estimated gain is below threshold"


@pytest.mark.unit
class TestRetryFailedToQueued:
    """Verify FAILED tasks are retried directly to QUEUED (not PENDING).

    Bug fix: previously _retry_failed_tasks set status to PENDING, but
    _dispatch_queued_tasks only picks up QUEUED tasks, causing retries
    to get stuck indefinitely.
    """

    def test_failed_can_transition_to_queued(self):
        """State machine must allow FAILED → QUEUED."""
        from app.models.task import VALID_TRANSITIONS
        assert TaskStatus.QUEUED in VALID_TRANSITIONS[TaskStatus.FAILED]

    def test_failed_cannot_transition_to_pending(self):
        """FAILED → PENDING should no longer be allowed."""
        from app.models.task import VALID_TRANSITIONS
        assert TaskStatus.PENDING not in VALID_TRANSITIONS[TaskStatus.FAILED]

    async def test_retry_sets_queued_not_pending(self, db_engine, monkeypatch):
        """_retry_failed_tasks must set FAILED tasks to QUEUED so the
        dispatcher picks them up on the next cycle."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        from app.config import settings
        monkeypatch.setattr(settings, "max_retry_count", 3)

        async with session_factory() as session:
            session.add(_file("f-retry", duration_sec=60.0))
            task = _task("t-retry", "f-retry", TaskStatus.QUEUED)
            task.status = TaskStatus.FAILED.value
            task.retry_count = 0
            task.error_code = "TRANSCRIBE_ERROR"
            task.error_message = "InvalidMessage: did not receive a valid HTTP response"
            session.add(task)
            await session.commit()

        runner = BackgroundTaskRunner()
        await runner._retry_failed_tasks()

        async with session_factory() as session:
            retried = (await session.execute(
                select(Task).where(Task.task_id == "t-retry")
            )).scalar_one()
            assert retried.status == TaskStatus.QUEUED.value, (
                f"Expected QUEUED after retry, got {retried.status}"
            )
            assert retried.retry_count == 1
            assert retried.assigned_server_id is None
            assert retried.error_code is None
            assert retried.error_message is None

    async def test_retry_respects_max_retry_count(self, db_engine, monkeypatch):
        """Tasks at max_retry_count should NOT be retried."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        from app.config import settings
        monkeypatch.setattr(settings, "max_retry_count", 3)

        async with session_factory() as session:
            session.add(_file("f-maxed", duration_sec=60.0))
            task = _task("t-maxed", "f-maxed", TaskStatus.QUEUED)
            task.status = TaskStatus.FAILED.value
            task.retry_count = 3
            session.add(task)
            await session.commit()

        runner = BackgroundTaskRunner()
        await runner._retry_failed_tasks()

        async with session_factory() as session:
            still_failed = (await session.execute(
                select(Task).where(Task.task_id == "t-maxed")
            )).scalar_one()
            assert still_failed.status == TaskStatus.FAILED.value
            assert still_failed.retry_count == 3

    async def test_retried_task_gets_dispatched(self, db_engine, monkeypatch):
        """End-to-end: FAILED → retry → QUEUED → dispatched by next cycle."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        from app.config import settings
        monkeypatch.setattr(settings, "max_retry_count", 3)

        async with session_factory() as session:
            session.add(_server("srv-retry", 2))
            session.add(_file("f-e2e", duration_sec=60.0))
            task = _task("t-e2e", "f-e2e", TaskStatus.QUEUED)
            task.status = TaskStatus.FAILED.value
            task.retry_count = 0
            task.error_message = "transient error"
            session.add(task)
            await session.commit()

        runner = BackgroundTaskRunner()
        started: list[str] = []
        monkeypatch.setattr(runner, "_execute_task", lambda tid: started.append(tid) or asyncio.sleep(0))

        await runner._retry_failed_tasks()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert "t-e2e" in started, "Retried task should be dispatched"

        async with session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.task_id == "t-e2e")
            )).scalar_one()
            assert task.status == TaskStatus.DISPATCHED.value
            assert task.assigned_server_id == "srv-retry"


@pytest.mark.unit
class TestSegmentedParentSlotDeadlock:
    """Fix for deterministic slot deadlock: parent tasks with pending-but-no-active
    segments must NOT occupy server slots, otherwise pending segments can never
    be dispatched and the system deadlocks.

    Reproduces the exact scenario from the 42-file batch test (2026-05-11):
    all 7 slots occupied by 7 parent tasks, each with pending segments that
    cannot be dispatched.
    """

    async def test_pending_segment_dispatched_when_parent_excluded_from_slots(
        self, db_engine, monkeypatch,
    ):
        """A parent in TRANSCRIBING with 1 succeeded + 1 pending segment on a
        1-slot server.  The parent must NOT occupy the slot so the pending
        segment can be dispatched."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1))

            session.add(_file("f-seg-parent", duration_sec=1200))
            parent = _task("t-seg-parent", "f-seg-parent", TaskStatus.TRANSCRIBING)
            parent.assigned_server_id = "srv-1"
            session.add(parent)

            session.add(TaskSegment(
                segment_id="seg-s1",
                task_id="t-seg-parent",
                segment_index=0,
                source_start_ms=0, source_end_ms=600000,
                keep_start_ms=0, keep_end_ms=600000,
                storage_path="/tmp/seg-s1.wav",
                status=SegmentStatus.SUCCEEDED,
                assigned_server_id="srv-1",
            ))
            session.add(TaskSegment(
                segment_id="seg-p1",
                task_id="t-seg-parent",
                segment_index=1,
                source_start_ms=600000, source_end_ms=1200000,
                keep_start_ms=600000, keep_end_ms=1200000,
                storage_path="/tmp/seg-p1.wav",
                status=SegmentStatus.PENDING,
            ))
            await session.commit()

        runner = BackgroundTaskRunner()
        started_segments: list[str] = []

        async def _fake_execute_segment(segment_id: str):
            started_segments.append(segment_id)

        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))
        monkeypatch.setattr(runner, "_execute_segment", _fake_execute_segment)

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert "seg-p1" in started_segments, (
            f"Pending segment should be dispatched, started: {started_segments}"
        )

    async def test_parent_and_regular_task_both_dispatch_with_enough_slots(
        self, db_engine, monkeypatch,
    ):
        """With 2 slots, a parent (not counting) + pending segment + regular
        task should both dispatch."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 2))

            session.add(_file("f-parent", duration_sec=600))
            parent = _task("t-parent", "f-parent", TaskStatus.TRANSCRIBING)
            parent.assigned_server_id = "srv-1"
            session.add(parent)

            session.add(TaskSegment(
                segment_id="seg-done",
                task_id="t-parent",
                segment_index=0,
                source_start_ms=0, source_end_ms=300000,
                keep_start_ms=0, keep_end_ms=300000,
                storage_path="/tmp/seg-done.wav",
                status=SegmentStatus.SUCCEEDED,
                assigned_server_id="srv-1",
            ))
            session.add(TaskSegment(
                segment_id="seg-pend",
                task_id="t-parent",
                segment_index=1,
                source_start_ms=300000, source_end_ms=600000,
                keep_start_ms=300000, keep_end_ms=600000,
                storage_path="/tmp/seg-pend.wav",
                status=SegmentStatus.PENDING,
            ))

            session.add(_file("f-regular", duration_sec=30))
            session.add(_task("t-regular", "f-regular", TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        started_tasks: list[str] = []
        started_segments: list[str] = []

        async def _fake_execute_segment(segment_id: str):
            started_segments.append(segment_id)

        monkeypatch.setattr(runner, "_execute_task",
                            lambda tid: started_tasks.append(tid) or asyncio.sleep(0))
        monkeypatch.setattr(runner, "_execute_segment", _fake_execute_segment)

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert "t-regular" in started_tasks, (
            f"Regular task should be dispatched, started tasks: {started_tasks}"
        )
        assert "seg-pend" in started_segments, (
            f"Pending segment should be dispatched, started segments: {started_segments}"
        )

    async def test_multiple_parents_all_slots_deadlock_resolved(
        self, db_engine, monkeypatch,
    ):
        """Reproduce the exact deadlock: N parent tasks fill N slots, each with
        pending segments.  After fix, pending segments should be dispatched."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 2))

            for i in range(2):
                fid = f"f-p{i}"
                tid = f"t-p{i}"
                session.add(_file(fid, duration_sec=600))
                parent = _task(tid, fid, TaskStatus.TRANSCRIBING)
                parent.assigned_server_id = "srv-1"
                session.add(parent)

                session.add(TaskSegment(
                    segment_id=f"seg-done-{i}",
                    task_id=tid,
                    segment_index=0,
                    source_start_ms=0, source_end_ms=300000,
                    keep_start_ms=0, keep_end_ms=300000,
                    storage_path=f"/tmp/seg-done-{i}.wav",
                    status=SegmentStatus.SUCCEEDED,
                    assigned_server_id="srv-1",
                ))
                session.add(TaskSegment(
                    segment_id=f"seg-pend-{i}",
                    task_id=tid,
                    segment_index=1,
                    source_start_ms=300000, source_end_ms=600000,
                    keep_start_ms=300000, keep_end_ms=600000,
                    storage_path=f"/tmp/seg-pend-{i}.wav",
                    status=SegmentStatus.PENDING,
                ))
            await session.commit()

        runner = BackgroundTaskRunner()
        started_segments: list[str] = []

        async def _fake_execute_segment(segment_id: str):
            started_segments.append(segment_id)

        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))
        monkeypatch.setattr(runner, "_execute_segment", _fake_execute_segment)

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert len(started_segments) >= 1, (
            f"At least 1 pending segment should be dispatched, got: {started_segments}"
        )


@pytest.mark.unit
class TestReplanCooldown:
    """Global replan should be rate-limited to avoid re-plan tornado."""

    async def test_replan_not_triggered_within_cooldown(self, db_engine, monkeypatch):
        """Adding new work within cooldown should NOT trigger full replan
        when an existing plan still has items in the queue."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1))
            for i in range(4):
                session.add(_file(f"f-{i}", duration_sec=60))
                session.add(_task(f"t-{i}", f"f-{i}", TaskStatus.QUEUED))
            await session.commit()

        replan_events: list[str] = []
        original_logger = __import__("app.services.task_runner", fromlist=["logger"]).logger

        class _CapturingLogger:
            def __getattr__(self, name):
                def _capture(event, **kw):
                    if event == "global_replan_triggered":
                        replan_events.append(kw.get("reason", ""))
                    return getattr(original_logger, name)(event, **kw)
                return _capture

        monkeypatch.setattr("app.services.task_runner.logger", _CapturingLogger())

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)
        assert len(replan_events) == 1, f"First dispatch should replan, got: {replan_events}"

        async with session_factory() as session:
            session.add(_file("f-new", duration_sec=60))
            session.add(_task("t-new", "f-new", TaskStatus.QUEUED))
            await session.commit()

        replan_events.clear()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert len(replan_events) == 0, (
            f"Second dispatch within cooldown should NOT replan, got reasons: {replan_events}"
        )

    async def test_replan_triggers_after_cooldown_expires(self, db_engine, monkeypatch):
        """After cooldown expires, new work items should trigger replan."""
        import time as _time

        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1))
            for i in range(4):
                session.add(_file(f"f-{i}", duration_sec=60))
                session.add(_task(f"t-{i}", f"f-{i}", TaskStatus.QUEUED))
            await session.commit()

        replan_events: list[str] = []
        original_logger = __import__("app.services.task_runner", fromlist=["logger"]).logger

        class _CapturingLogger:
            def __getattr__(self, name):
                def _capture(event, **kw):
                    if event == "global_replan_triggered":
                        replan_events.append(kw.get("reason", ""))
                    return getattr(original_logger, name)(event, **kw)
                return _capture

        monkeypatch.setattr("app.services.task_runner.logger", _CapturingLogger())

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)
        assert len(replan_events) == 1

        runner._last_replan_time = _time.monotonic() - 10.0

        async with session_factory() as session:
            session.add(_file("f-new", duration_sec=60))
            session.add(_task("t-new", "f-new", TaskStatus.QUEUED))
            await session.commit()

        replan_events.clear()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert len(replan_events) == 1, (
            f"After cooldown, new work should trigger replan, got: {replan_events}"
        )
        assert replan_events[0] == "new_work_items"

    async def test_incremental_merge_does_not_leapfrog_existing_backlog(self, db_engine, monkeypatch):
        """New tasks merged during cooldown must sort after existing PlanPool backlog,
        not jump to the front due to estimated_finish being calculated without backlog offset."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 2))
            for i in range(6):
                session.add(_file(f"f-backlog-{i}", duration_sec=120))
                session.add(_task(f"t-backlog-{i}", f"f-backlog-{i}", TaskStatus.QUEUED))
            await session.commit()

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        pool_before = {
            sid: runner._plan_pool.get_queue_snapshot(sid)
            for sid in runner._plan_pool.server_ids
        }
        old_task_ids = set()
        max_old_finish = 0.0
        for q in pool_before.values():
            for d in q:
                old_task_ids.add(d.task_id)
                max_old_finish = max(max_old_finish, d.estimated_finish)

        async with session_factory() as session:
            session.add(_file("f-new-inc", duration_sec=30))
            session.add(_task("t-new-inc", "f-new-inc", TaskStatus.QUEUED))
            await session.commit()

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        pool_after = {
            sid: runner._plan_pool.get_queue_snapshot(sid)
            for sid in runner._plan_pool.server_ids
        }

        for sid, q in pool_after.items():
            for i, d in enumerate(q):
                if d.task_id == "t-new-inc":
                    preceding_old = [q[j] for j in range(i) if q[j].task_id in old_task_ids]
                    assert d.estimated_finish >= max_old_finish or len(preceding_old) == 0, (
                        f"New task t-new-inc (finish={d.estimated_finish:.1f}) leapfrogged "
                        f"old backlog (max_old_finish={max_old_finish:.1f}) on {sid}"
                    )


@pytest.mark.unit
class TestQueueImbalanceDenoising:
    """Queue imbalance with low remaining backlog should prefer steal over replan."""

    def test_low_backlog_does_not_trigger_imbalance(self):
        from app.services.scheduler import ScheduleDecision

        runner = BackgroundTaskRunner()
        runner._planned_available_server_ids = frozenset({"srv-1", "srv-2"})
        runner._plan_pool.merge([
            ScheduleDecision("t-1", "srv-1", 0, 0.0, 10.0, 10.0, 1, 20.0),
        ])

        result = runner._check_queue_imbalance(frozenset({"srv-1", "srv-2"}))
        assert result is False, "Low backlog (<30s) should NOT trigger replan"

    def test_high_backlog_triggers_true_imbalance(self):
        from app.services.scheduler import ScheduleDecision

        runner = BackgroundTaskRunner()
        runner._planned_available_server_ids = frozenset({"srv-1", "srv-2"})
        runner._plan_pool.merge([
            ScheduleDecision("t-1", "srv-1", 0, 0.0, 200.0, 200.0, 1, 500.0),
            ScheduleDecision("t-2", "srv-1", 0, 200.0, 200.0, 400.0, 2, 500.0),
        ])

        result = runner._check_queue_imbalance(frozenset({"srv-1", "srv-2"}))
        assert result is True, "High backlog with exhausted server should trigger replan"


@pytest.mark.unit
class TestTrueActiveSlotCounting:
    """Slot counting must count real work only: whole-file tasks + active segments."""

    async def test_segmented_parent_does_not_count_as_server_slot(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_server("srv-1", 4))

            session.add(_file("f-whole", duration_sec=60))
            whole = _task("t-whole", "f-whole", TaskStatus.TRANSCRIBING)
            whole.assigned_server_id = "srv-1"
            session.add(whole)

            session.add(_file("f-parent", duration_sec=1200))
            parent = _task("t-parent", "f-parent", TaskStatus.TRANSCRIBING)
            parent.assigned_server_id = "srv-1"
            session.add(parent)

            session.add(TaskSegment(
                segment_id="seg-active",
                task_id="t-parent",
                segment_index=0,
                source_start_ms=0,
                source_end_ms=600000,
                keep_start_ms=0,
                keep_end_ms=600000,
                storage_path="/tmp/seg-active.wav",
                status=SegmentStatus.TRANSCRIBING,
                assigned_server_id="srv-1",
            ))
            await session.commit()

            counts = await BackgroundTaskRunner()._count_server_active_work(session)

        assert counts == {"srv-1": 2}

    async def test_dispatching_segment_parent_does_not_log_false_overcommit(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1))

            session.add(_file("f-parent", duration_sec=1200))
            parent = _task("t-parent", "f-parent", TaskStatus.QUEUED)
            session.add(parent)
            session.add(TaskSegment(
                segment_id="seg-pending",
                task_id="t-parent",
                segment_index=0,
                source_start_ms=0,
                source_end_ms=600000,
                keep_start_ms=0,
                keep_end_ms=600000,
                storage_path="/tmp/seg-pending.wav",
                status=SegmentStatus.PENDING,
            ))
            await session.commit()

        logged_errors: list[str] = []
        original_logger = __import__("app.services.task_runner", fromlist=["logger"]).logger

        class _CapturingLogger:
            def __getattr__(self, name):
                def _capture(event, **kw):
                    if name == "error":
                        logged_errors.append(event)
                    return getattr(original_logger, name)(event, **kw)
                return _capture

        monkeypatch.setattr("app.services.task_runner.logger", _CapturingLogger())

        runner = BackgroundTaskRunner()
        started_segments: list[str] = []

        async def _fake_execute_segment(segment_id: str):
            started_segments.append(segment_id)

        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))
        monkeypatch.setattr(runner, "_execute_segment", _fake_execute_segment)

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        assert started_segments == ["seg-pending"]
        assert "slot_overcommit_invariant_violated" not in logged_errors

    async def test_real_post_dispatch_overcommit_rolls_back_and_does_not_start(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            session.add(_server("srv-1", 1))
            session.add(_file("f-over", duration_sec=60))
            session.add(_task("t-over", "f-over", TaskStatus.QUEUED))
            await session.commit()

        logged_errors: list[str] = []
        original_logger = __import__("app.services.task_runner", fromlist=["logger"]).logger

        class _CapturingLogger:
            def __getattr__(self, name):
                def _capture(event, **kw):
                    if name == "error":
                        logged_errors.append(event)
                    return getattr(original_logger, name)(event, **kw)
                return _capture

        monkeypatch.setattr("app.services.task_runner.logger", _CapturingLogger())

        runner = BackgroundTaskRunner()
        count_calls = 0

        async def _fake_count_active(_session):
            nonlocal count_calls
            count_calls += 1
            if count_calls == 1:
                return {}
            return {"srv-1": 2}

        started_tasks: list[str] = []
        monkeypatch.setattr(runner, "_count_server_active_work", _fake_count_active)
        monkeypatch.setattr(runner, "_execute_task",
                            lambda tid: started_tasks.append(tid) or asyncio.sleep(0))

        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.task_id == "t-over")
            )).scalar_one()

        assert task.status == TaskStatus.QUEUED.value
        assert task.assigned_server_id is None
        assert started_tasks == []
        assert "slot_overcommit_dispatch_blocked" in logged_errors


@pytest.mark.unit
class TestPreprocessingClaimTimeout:
    """Bug #6: stale preprocessing claims should be released."""

    async def test_stale_preprocessing_claim_released(self, db_engine, monkeypatch):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import update as sql_update

        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        stale_started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

        async with session_factory() as session:
            session.add(_file("f-prep", duration_sec=30))
            task = _task("t-prep", "f-prep", TaskStatus.PREPROCESSING)
            session.add(task)
            await session.commit()
            await session.execute(
                sql_update(Task)
                .where(Task.task_id == "t-prep")
                .values(started_at=stale_started_at)
            )
            await session.commit()

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_create_segments_for_task",
                            lambda *a, **kw: asyncio.sleep(0))
        await runner._promote_preprocessing_tasks()

        async with session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.task_id == "t-prep")
            )).scalar_one()

        assert task.started_at is None, \
            "Stale preprocessing claim should have been released"


@pytest.mark.unit
class TestFrozenTaskDetection:
    """Bug #12: detect tasks frozen in TRANSCRIBING longer than timeout."""

    async def test_frozen_task_detected_without_error(self, db_engine, monkeypatch):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import update as sql_update
        from unittest.mock import AsyncMock

        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        frozen_started = datetime.now(timezone.utc) - timedelta(seconds=7200)

        async with session_factory() as session:
            session.add(_server("srv-frozen", 4))
            session.add(_file("f-frozen", duration_sec=600))
            task = _task("t-frozen", "f-frozen", TaskStatus.TRANSCRIBING)
            task.assigned_server_id = "srv-frozen"
            session.add(task)
            await session.commit()
            await session.execute(
                sql_update(Task)
                .where(Task.task_id == "t-frozen")
                .values(started_at=frozen_started)
            )
            await session.commit()

        logged_events: list[str] = []
        original_logger = __import__("app.services.task_runner", fromlist=["logger"]).logger

        class _CapturingLogger:
            def __getattr__(self, name):
                def _capture(event, **kw):
                    logged_events.append(event)
                    return getattr(original_logger, name)(event, **kw)
                return _capture

        monkeypatch.setattr("app.services.task_runner.logger", _CapturingLogger())

        runner = BackgroundTaskRunner()
        await runner._detect_frozen_tasks()

        assert "progress_frozen_detected" in logged_events, \
            f"Expected progress_frozen_detected, got: {logged_events}"


@pytest.mark.unit
class TestDisabledServerWarning:
    """Bug #8: servers with enabled=false should trigger a warning log."""

    async def test_disabled_server_excludes_from_dispatch(self, db_engine, monkeypatch):
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        async with session_factory() as session:
            srv = _server("srv-disabled", 4)
            srv.enabled = False
            session.add(srv)
            session.add(_file("f-dis", duration_sec=30))
            session.add(_task("t-dis", "f-dis", TaskStatus.QUEUED))
            await session.commit()

        logged_events: list[str] = []
        original_logger = __import__("app.services.task_runner", fromlist=["logger"]).logger

        class _CapturingLogger:
            def __getattr__(self, name):
                def _capture(event, **kw):
                    logged_events.append(event)
                    return getattr(original_logger, name)(event, **kw)
                return _capture

        monkeypatch.setattr("app.services.task_runner.logger", _CapturingLogger())

        runner = BackgroundTaskRunner()
        monkeypatch.setattr(runner, "_execute_task", lambda tid: asyncio.sleep(0))

        await runner._dispatch_queued_tasks()

        assert "servers_disabled_excluded_from_dispatch" in logged_events, \
            f"Expected disabled server warning, got: {logged_events}"
        async with session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.task_id == "t-dis")
            )).scalar_one()
        assert task.status == TaskStatus.QUEUED.value, \
            "Task should remain QUEUED when no enabled servers exist"


@pytest.mark.unit
class TestEmptyResultQualityGate:
    """Long audio with empty ASR output should retry instead of succeeding."""

    async def test_long_whole_file_empty_result_marks_task_failed_for_retry(self, db_engine, monkeypatch):
        from app.adapters.base import ParsedResult

        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        class _Adapter:
            async def transcribe(self, **kwargs):
                return ParsedResult(text="", raw={"text": ""})

        monkeypatch.setattr("app.services.task_runner.get_adapter", lambda **kw: _Adapter())

        async def _save_result_should_not_run(*args, **kwargs):
            raise AssertionError("Empty long-audio result should not be saved as success")

        monkeypatch.setattr("app.services.task_runner.save_result", _save_result_should_not_run)

        async with session_factory() as session:
            session.add(_server("srv-empty", 1))
            session.add(_file("f-empty", duration_sec=120))
            task = _task("t-empty", "f-empty", TaskStatus.DISPATCHED)
            task.assigned_server_id = "srv-empty"
            session.add(task)
            await session.commit()

        await BackgroundTaskRunner()._execute_task("t-empty")

        async with session_factory() as session:
            task = (await session.execute(
                select(Task).where(Task.task_id == "t-empty")
            )).scalar_one()

        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "EMPTY_RESULT"
        assert task.result_path is None

    async def test_long_segment_empty_result_requeues_segment_for_retry(self, db_engine, monkeypatch):
        from app.adapters.base import ParsedResult

        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)
        monkeypatch.setattr("app.services.task_runner.breaker_registry", _breaker_mock())

        class _Adapter:
            async def transcribe(self, **kwargs):
                return ParsedResult(text="", raw={"text": ""})

        monkeypatch.setattr("app.services.task_runner.get_adapter", lambda **kw: _Adapter())

        async with session_factory() as session:
            session.add(_server("srv-empty", 1))
            session.add(_file("f-parent-empty", duration_sec=1200))
            parent = _task("t-parent-empty", "f-parent-empty", TaskStatus.DISPATCHED)
            parent.assigned_server_id = "srv-empty"
            session.add(parent)
            session.add(TaskSegment(
                segment_id="seg-empty",
                task_id="t-parent-empty",
                segment_index=0,
                source_start_ms=0,
                source_end_ms=600000,
                keep_start_ms=0,
                keep_end_ms=600000,
                storage_path="/tmp/seg-empty.wav",
                status=SegmentStatus.DISPATCHED,
                assigned_server_id="srv-empty",
            ))
            await session.commit()

        await BackgroundTaskRunner()._execute_segment("seg-empty")

        async with session_factory() as session:
            segment = (await session.execute(
                select(TaskSegment).where(TaskSegment.segment_id == "seg-empty")
            )).scalar_one()

        assert segment.status == SegmentStatus.PENDING.value
        assert segment.retry_count == 1
        assert "Empty ASR result" in (segment.error_message or "")
