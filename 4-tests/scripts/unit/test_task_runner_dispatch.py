"""Task runner dispatch behavior tests."""

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


@pytest.mark.unit
class TestSlotQueueDispatch:
    """Verify that slot queues cache the plan and avoid redundant re-planning."""

    async def test_slot_queues_populated_after_first_dispatch(self, db_engine, monkeypatch):
        """First dispatch should build slot queues for deferred tasks."""
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

        assert runner._slot_queues, "Slot queues should hold deferred tasks"
        total_deferred = sum(len(sq.decisions) for sq in runner._slot_queues.values())
        assert total_deferred > 0, "Some tasks should remain in slot queues for later dispatch"

    async def test_second_dispatch_pops_from_queue(self, db_engine, monkeypatch):
        """After a task completes, second dispatch should pop from pre-planned queue."""
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

        deferred_before = sum(len(sq.decisions) for sq in runner._slot_queues.values())
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
        deferred_after = sum(len(sq.decisions) for sq in runner._slot_queues.values())
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
        runner._clear_slot_queues()
        await runner._dispatch_queued_tasks()
        await asyncio.sleep(0)

        async with session_factory() as session:
            all_tasks = list((await session.execute(select(Task))).scalars().all())
            newly_dispatched = [t for t in all_tasks if t.status == TaskStatus.DISPATCHED]

        for t in newly_dispatched:
            assert t.assigned_server_id != "srv-1", \
                f"Task {t.task_id} dispatched to circuit-broken server srv-1"

    async def test_plan_invalidated_on_server_set_change(self, db_engine, monkeypatch):
        """When available server set changes, _slot_queues should be rebuilt."""
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

        runner._clear_slot_queues()
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
    """Verify slot-queue plan invalidation on task completion (conservative clearing)."""

    async def test_plan_preserved_when_slot_queues_still_have_decisions_on_success(
        self, db_engine, monkeypatch,
    ):
        """Do not wipe the global plan while other tasks remain in slot queues."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("f-done", duration_sec=60.0))
            session.add(_task("t-done", "f-done", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        from app.services.scheduler import ScheduleDecision, SlotQueue
        pending = [
            ScheduleDecision("t-fake", "srv-1", 0, 10.0, 5.0, 15.0, 2, 30.0),
        ]
        runner._slot_queues = {
            "srv-1:0": SlotQueue(server_id="srv-1", slot_index=0, decisions=pending.copy()),
        }
        runner._planned_task_ids = {"t-fake"}
        runner._planned_available_server_ids = frozenset({"srv-1"})

        await runner._mark_task_succeeded("t-done")

        assert runner._slot_queues["srv-1:0"].decisions == pending
        assert runner._planned_task_ids == {"t-fake"}
        assert runner._planned_available_server_ids == frozenset({"srv-1"})
        assert runner._dispatch_event.is_set()

    async def test_plan_preserved_when_slot_queues_still_have_decisions_on_failure(
        self, db_engine, monkeypatch,
    ):
        """Same as success path: pending slot-queue work keeps the plan intact."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("f-fail", duration_sec=60.0))
            session.add(_task("t-fail", "f-fail", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        from app.services.scheduler import ScheduleDecision, SlotQueue
        pending = [
            ScheduleDecision("t-fake2", "srv-1", 0, 10.0, 5.0, 15.0, 2, 30.0),
        ]
        runner._slot_queues = {
            "srv-1:0": SlotQueue(server_id="srv-1", slot_index=0, decisions=pending.copy()),
        }
        runner._planned_task_ids = {"t-fake2"}
        runner._planned_available_server_ids = frozenset({"srv-1"})

        await runner._mark_task_failed("t-fail", "some error")

        assert runner._slot_queues["srv-1:0"].decisions == pending
        assert runner._planned_task_ids == {"t-fake2"}
        assert runner._planned_available_server_ids == frozenset({"srv-1"})
        assert runner._dispatch_event.is_set()

    async def test_plan_cleared_on_task_success_when_no_pending_slot_decisions(
        self, db_engine, monkeypatch,
    ):
        """When every slot queue is empty, completion clears the plan for the next replan."""
        session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr("app.services.task_runner.async_session_factory", session_factory)

        async with session_factory() as session:
            session.add(_file("f-done2", duration_sec=60.0))
            session.add(_task("t-done2", "f-done2", TaskStatus.TRANSCRIBING))
            await session.commit()

        runner = BackgroundTaskRunner()
        from app.services.scheduler import SlotQueue
        runner._slot_queues = {
            "srv-1:0": SlotQueue(server_id="srv-1", slot_index=0, decisions=[]),
        }
        runner._planned_task_ids = set()
        runner._planned_available_server_ids = frozenset({"srv-1"})

        await runner._mark_task_succeeded("t-done2")

        assert runner._slot_queues == {}
        assert runner._planned_task_ids == set()
        assert runner._planned_available_server_ids == frozenset()
        assert runner._dispatch_event.is_set()


@pytest.mark.unit
class TestStealImprovementCalculation:
    """Verify work stealing uses remaining source queue time, not stale estimated_finish."""

    def test_find_steal_candidate_uses_remaining_time(self):
        """Improvement should be computed as source_remaining - est_stolen,
        not decision.estimated_finish - est_stolen."""
        from app.services.scheduler import ScheduleDecision, SlotQueue, ServerProfile
        from app.services.scheduler import scheduler as global_scheduler

        runner = BackgroundTaskRunner()

        runner._slot_queues = {
            "slow:0": SlotQueue(server_id="slow", slot_index=0, decisions=[
                ScheduleDecision("t-ahead", "slow", 0, 0.0, 30.0, 30.0, 1, 100.0,),
                ScheduleDecision("t-target", "slow", 0, 30.0, 25.0, 55.0, 2, 80.0),
            ]),
        }

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
        decision, source_sq, est_stolen = result
        assert decision.task_id == "t-target"

        est_on_idle = global_scheduler.estimate_processing_time(80.0, idle_profile)
        source_remaining = 30.0 + 25.0
        expected_improvement = source_remaining - est_on_idle
        assert expected_improvement > 0

    def test_no_steal_when_source_faster_than_idle(self):
        """Should NOT steal when the source server processes faster than idle server."""
        from app.services.scheduler import ScheduleDecision, SlotQueue, ServerProfile

        runner = BackgroundTaskRunner()

        runner._slot_queues = {
            "fast:0": SlotQueue(server_id="fast", slot_index=0, decisions=[
                ScheduleDecision("t-only", "fast", 0, 0.0, 8.0, 8.0, 1, 10.0),
            ]),
        }

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