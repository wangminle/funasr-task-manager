"""Background task runner for queued ASR jobs."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update as sql_update
from sqlalchemy.orm import selectinload
from ulid import ULID

from app.adapters.base import MessageProfile
from app.adapters.registry import get_adapter
from app.config import SEGMENT_LEVEL_PRESETS, settings
from app.fault.circuit_breaker import breaker_registry
from app.models import (
    File, ServerInstance, Task, TaskStatus,
    TaskEvent, TaskSegment, SegmentStatus,
)
from app.observability.logging import get_logger
from app.services.audio_preprocessor import (
    ensure_canonical_wav, ensure_wav, get_audio_duration_ms,
    needs_conversion, plan_segments, silence_detect, split_wav_segments,
)
from app.services.callback_worker import callback_worker
from app.services.result_formatter import to_json, to_srt, to_txt
from app.services.scheduler import PlanPool, ScheduleDecision, ServerProfile, scheduler as global_scheduler
from app.services.task_finalizer import TaskFinalizer
from app.storage.database import async_session_factory
from app.storage.file_manager import save_result
from app.storage.repository import SegmentRepository, TaskRepository
from app.auth.rate_limiter import rate_limiter

logger = get_logger(__name__)


REPLAN_IMBALANCE_RATIO = 1.5
REPLAN_COOLDOWN_SEC = 5.0
SERVERS_CHANGED_COOLDOWN_SEC = 3.0
EMPTY_RESULT_RETRY_MIN_AUDIO_SEC = 30.0
EMPTY_RESULT_RETRY_MIN_SEGMENT_SEC = 10.0


class BackgroundTaskRunner:
    """Polls tasks and executes transcription jobs in-process."""

    def __init__(self, poll_interval: float = 1.0, preprocessing_delay_seconds: int = 2):
        self.poll_interval = poll_interval
        self.preprocessing_delay_seconds = preprocessing_delay_seconds
        self._loop_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._dispatch_event = asyncio.Event()
        self._dispatch_lock = asyncio.Lock()
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        self._plan_pool = PlanPool()
        self._planned_available_server_ids: frozenset[str] = frozenset()
        self._last_replan_time: float = 0.0
        self._callback_worker = callback_worker
        self._finalizer = TaskFinalizer(self._callback_worker)

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        await self._recover_orphaned_segments()
        self._stop_event.clear()
        self._dispatch_event.set()
        self._loop_task = asyncio.create_task(self._run_loop(), name="asr-background-task-runner")
        logger.info("task_runner_started")

    async def _recover_orphaned_segments(self) -> None:
        """Recover segments and tasks that were left in non-terminal states.

        Handles two crash-recovery scenarios:

        1. **Orphaned active segments**: DISPATCHED/TRANSCRIBING segments
           whose in-memory coroutines were lost. Reset them to PENDING so
           the dispatcher picks them up again.

        2. **Stalled finalization**: All segments SUCCEEDED but the parent
           task never reached a terminal state (crash between last segment
           completion and merge/finalize). Trigger finalize for these tasks.
        """
        finalize_task_ids: list[str] = []
        try:
            async with async_session_factory() as session:
                seg_repo = SegmentRepository(session)

                # --- Scenario 1: reset orphaned active segments ---
                stmt = (
                    select(TaskSegment)
                    .where(TaskSegment.status.in_([
                        SegmentStatus.DISPATCHED,
                        SegmentStatus.TRANSCRIBING,
                    ]))
                )
                orphans = list((await session.execute(stmt)).scalars().all())
                if orphans:
                    for seg in orphans:
                        seg.status = SegmentStatus.PENDING
                        seg.assigned_server_id = None
                        seg.error_message = None
                        seg.started_at = None
                    await session.commit()
                    logger.info("recovered_orphaned_segments", count=len(orphans))

                # --- Scenario 2: find stalled parents needing finalization ---
                parent_ids_stmt = (
                    select(TaskSegment.task_id).distinct()
                )
                all_parent_ids = set(
                    (await session.execute(parent_ids_stmt)).scalars().all()
                )
                for pid in all_parent_ids:
                    repo = TaskRepository(session)
                    task = await repo.get_task(pid)
                    if task is None:
                        continue
                    if task.status in (
                        TaskStatus.SUCCEEDED.value,
                        TaskStatus.CANCELED.value,
                    ):
                        continue
                    counts = await seg_repo.count_by_status(pid)
                    total = sum(counts.values())
                    if total == 0:
                        continue
                    succeeded = counts.get(SegmentStatus.SUCCEEDED, 0)
                    if succeeded == total:
                        finalize_task_ids.append(pid)
                        logger.info("stalled_finalization_detected", task_id=pid)
        except Exception as e:
            logger.warning("recover_orphaned_segments_failed", error=str(e))

        for tid in finalize_task_ids:
            try:
                await self._finalizer.maybe_finalize(tid)
                logger.info("stalled_finalization_triggered", task_id=tid)
            except Exception as e:
                logger.warning("stalled_finalization_failed", task_id=tid, error=str(e))

    async def stop(self) -> None:
        if self._loop_task is None:
            return
        self._stop_event.set()
        self._dispatch_event.set()
        await self._loop_task
        self._loop_task = None
        logger.info("task_runner_stopped")

    async def _run_loop(self) -> None:
        retry_tick = 0
        callback_tick = 0
        freeze_tick = 0
        while not self._stop_event.is_set():
            try:
                await self._promote_preprocessing_tasks()
                await self._dispatch_queued_tasks()
                retry_tick += 1
                if retry_tick >= 10:
                    await self._retry_failed_tasks()
                    retry_tick = 0
                callback_tick += 1
                if callback_tick >= 30:
                    await self._callback_worker.retry_pending()
                    callback_tick = 0
                freeze_tick += 1
                if freeze_tick >= 60:
                    await self._detect_frozen_tasks()
                    freeze_tick = 0
            except Exception as e:
                logger.exception("task_runner_loop_error", error=str(e))
            await self._wait_for_dispatch_signal()

    async def _retry_failed_tasks(self) -> None:
        """Re-queue FAILED tasks for automatic retry (up to max_retry_count).

        Goes directly FAILED → QUEUED so the dispatcher picks them up
        immediately. The file is already uploaded and preprocessed, so
        there is no need to revisit PENDING/PREPROCESSING.

        Three retry paths for segmented tasks:
        - MERGE_FAILED: push parent back to TRANSCRIBING and re-trigger finalize.
        - SEGMENT_RETRY_EXHAUSTED: delete old segment records and re-queue as
          whole-file (one fallback attempt before giving up).
        - Other segment errors: skip (segment-level retry already exhausted).
        """
        max_retries = settings.max_retry_count
        merge_retry_task_ids: list[str] = []
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            seg_repo = SegmentRepository(session)
            stmt = (
                select(Task)
                .where(Task.status == TaskStatus.FAILED, Task.retry_count < max_retries)
                .order_by(Task.created_at.asc())
                .limit(50)
            )
            tasks = list((await session.execute(stmt)).scalars().all())
            if not tasks:
                return
            for task in tasks:
                seg_counts = await seg_repo.count_by_status(task.task_id)
                has_segments = sum(seg_counts.values()) > 0
                if has_segments:
                    if task.error_code == "MERGE_FAILED" and task.can_transition_to(TaskStatus.TRANSCRIBING):
                        task.retry_count += 1
                        task.error_code = None
                        task.error_message = None
                        await repo.update_task_status(task, TaskStatus.TRANSCRIBING)
                        merge_retry_task_ids.append(task.task_id)
                        logger.info("merge_retry_queued",
                                    task_id=task.task_id, retry=task.retry_count)
                    elif (task.error_code == "SEGMENT_RETRY_EXHAUSTED"
                          and task.can_transition_to(TaskStatus.QUEUED)):
                        deleted = await seg_repo.delete_segments_by_task(task.task_id)
                        task.retry_count += 1
                        task.assigned_server_id = None
                        task.error_code = None
                        task.error_message = None
                        task.started_at = None
                        task.completed_at = None
                        await repo.update_task_status(task, TaskStatus.QUEUED)
                        logger.info("whole_file_fallback_queued",
                                    task_id=task.task_id,
                                    retry=task.retry_count,
                                    deleted_segments=deleted,
                                    hint="All segments failed; retrying as whole-file")
                    continue
                if task.can_transition_to(TaskStatus.QUEUED):
                    task.retry_count += 1
                    task.assigned_server_id = None
                    task.error_code = None
                    task.error_message = None
                    task.started_at = None
                    task.completed_at = None
                    await repo.update_task_status(task, TaskStatus.QUEUED)
                    logger.info("task_retry_queued", task_id=task.task_id, retry=task.retry_count)
            await session.commit()
        for tid in merge_retry_task_ids:
            await self._finalizer.maybe_finalize(tid)
        self._request_dispatch()

    async def _detect_frozen_tasks(self) -> None:
        """Detect tasks stuck in TRANSCRIBING longer than expected.

        Logs a warning for each frozen task.  Does not force-fail them
        (the WebSocket read_idle_timeout and dynamic timeout handle that);
        this is a diagnostic safety net.
        """
        freeze_threshold = timedelta(seconds=settings.task_timeout_seconds)
        now = datetime.now(timezone.utc)
        try:
            async with async_session_factory() as session:
                stmt = (
                    select(Task)
                    .where(
                        Task.status == TaskStatus.TRANSCRIBING,
                        Task.started_at.is_not(None),
                    )
                )
                candidates = list((await session.execute(stmt)).scalars().all())
                for task in candidates:
                    started = task.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if started >= now - freeze_threshold:
                        continue
                    elapsed = (now - started).total_seconds()
                    logger.error(
                        "progress_frozen_detected",
                        task_id=task.task_id,
                        server_id=task.assigned_server_id,
                        started_at=started.isoformat(),
                        elapsed_seconds=int(elapsed),
                        threshold_seconds=settings.task_timeout_seconds,
                        hint="Task has been TRANSCRIBING longer than "
                             "task_timeout_seconds with no progress update",
                    )

                seg_stmt = (
                    select(TaskSegment)
                    .where(
                        TaskSegment.status == SegmentStatus.TRANSCRIBING,
                        TaskSegment.started_at.is_not(None),
                    )
                )
                seg_candidates = list((await session.execute(seg_stmt)).scalars().all())
                for seg in seg_candidates:
                    started = seg.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    if started >= now - freeze_threshold:
                        continue
                    elapsed = (now - started).total_seconds()
                    logger.error(
                        "segment_progress_frozen_detected",
                        segment_id=seg.segment_id,
                        task_id=seg.task_id,
                        server_id=seg.assigned_server_id,
                        elapsed_seconds=int(elapsed),
                    )
        except Exception as e:
            logger.warning("frozen_task_detection_failed", error=str(e))

    async def _promote_preprocessing_tasks(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.preprocessing_delay_seconds)
        claim_time = datetime.now(timezone.utc)

        # Release orphaned claims: if a previous runner crashed mid-preprocessing,
        # started_at would remain set.  After 5 minutes, release the claim so the
        # task can be re-processed.
        claim_stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
        async with async_session_factory() as session:
            stale_result = await session.execute(
                sql_update(Task)
                .where(
                    Task.status == TaskStatus.PREPROCESSING,
                    Task.started_at.is_not(None),
                    Task.started_at < claim_stale_cutoff,
                )
                .values(started_at=None)
            )
            if stale_result.rowcount > 0:
                await session.commit()
                logger.warning(
                    "released_stale_preprocessing_claims",
                    count=stale_result.rowcount,
                    stale_minutes=5,
                )

        candidates: list[tuple[str, float, str, str | None]] = []
        async with async_session_factory() as session:
            stmt = (
                select(Task)
                .options(selectinload(Task.file))
                .where(
                    Task.status == TaskStatus.PREPROCESSING,
                    Task.created_at <= cutoff,
                    Task.started_at.is_(None),
                )
                .order_by(Task.created_at.asc())
                .limit(100)
            )
            tasks = list((await session.execute(stmt)).scalars().all())
            if not tasks:
                return
            for task in tasks:
                if not task.can_transition_to(TaskStatus.QUEUED):
                    continue
                # Atomic claim via started_at (no FK constraint, unlike
                # assigned_server_id).  Other runners see started_at IS NOT
                # NULL and skip the task.
                claim_result = await session.execute(
                    sql_update(Task)
                    .where(
                        Task.task_id == task.task_id,
                        Task.status == TaskStatus.PREPROCESSING,
                        Task.started_at.is_(None),
                    )
                    .values(started_at=claim_time)
                )
                if claim_result.rowcount != 1:
                    continue
                await session.commit()
                duration_sec = task.file.duration_sec if task.file and task.file.duration_sec else 0
                audio_path = task.file.storage_path if task.file else ""
                candidates.append((task.task_id, duration_sec or 0, audio_path, task.options_json))

        if not candidates:
            return

        for task_id, duration_sec, audio_path, options_json in candidates:
            seg_level = self._parse_segment_level(options_json)

            if seg_level == "off":
                needs_segmentation = False
            else:
                preset = SEGMENT_LEVEL_PRESETS.get(seg_level) if seg_level != "10m" else None
                min_file_dur = preset.min_file_duration_sec if preset else settings.segment_min_file_duration_sec
                needs_segmentation = (
                    settings.segment_enabled
                    and duration_sec >= min_file_dur
                    and bool(audio_path)
                )

            if needs_segmentation:
                try:
                    await self._create_segments_for_task(task_id, audio_path, seg_level=seg_level)
                except Exception as e:
                    logger.error(
                        "segmentation_failed_marking_task_failed",
                        task_id=task_id,
                        segment_level=seg_level,
                        duration_sec=duration_sec,
                        error=str(e),
                        hint="Long audio requires segmentation; refusing unsafe whole-file fallback",
                    )
                    await self._handle_segmentation_failure(task_id, str(e))
                    continue

            try:
                async with async_session_factory() as session:
                    repo = TaskRepository(session)
                    task = await repo.get_task(task_id)
                    if task and task.can_transition_to(TaskStatus.QUEUED):
                        task.started_at = None
                        await repo.update_task_status(task, TaskStatus.QUEUED)
                        await session.commit()
            except Exception as e:
                logger.warning("promote_task_failed", task_id=task_id, error=str(e))

        self._request_dispatch()

    async def _handle_segmentation_failure(self, task_id: str, error: str) -> None:
        """Retry long-audio segmentation failures before failing the task."""
        pending_delivery = None
        completed_user_id: str | None = None
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if not task:
                return
            task.started_at = None
            task.assigned_server_id = None

            if task.retry_count < settings.max_retry_count:
                task.retry_count += 1
                task.error_code = "SEGMENTATION_RETRY_PENDING"
                task.error_message = (
                    "Long-audio segmentation failed before dispatch; retrying segmentation "
                    f"without whole-file fallback: {error}"
                )
                logger.info(
                    "segmentation_retry_queued",
                    task_id=task_id,
                    retry=task.retry_count,
                    max_retries=settings.max_retry_count,
                )
            elif task.can_transition_to(TaskStatus.FAILED):
                error_message = (
                    "Long-audio segmentation failed before dispatch after retries; "
                    f"whole-file fallback is disabled to avoid ASR server timeout: {error}"
                )
                task.error_code = "SEGMENTATION_FAILED"
                task.error_message = error_message
                event = await repo.update_task_status(task, TaskStatus.FAILED)
                outbox = await self._callback_worker.enqueue(
                    session,
                    task,
                    event.event_id,
                    TaskStatus.FAILED,
                    error_message=error_message,
                )
                if outbox is not None:
                    pending_delivery = (outbox.outbox_id, task.callback_secret)
                completed_user_id = task.user_id
            await session.commit()
        if completed_user_id:
            await rate_limiter.record_task_completed(completed_user_id)
        if not self._plan_pool:
            self._clear_plan_pool(reset_server_ids=False)
        self._request_dispatch()
        if pending_delivery:
            await self._callback_worker.try_deliver(*pending_delivery)

    async def _create_segments_for_task(
        self, task_id: str, audio_path: str, *, seg_level: str = "10m",
    ) -> None:
        """Execute canonical WAV → silence detect → plan → split → write segments.

        Heavy I/O (ffmpeg) happens outside any database session.  Writing
        segment records uses a short dedicated session with an idempotency
        guard to handle runner restarts safely.

        On failure, only the worker-specific temp directory is cleaned up;
        the published segment directory is never removed if DB records
        already reference it.
        """
        import shutil
        from pathlib import Path
        from sqlalchemy.exc import IntegrityError
        from ulid import ULID

        async with async_session_factory() as session:
            seg_repo = SegmentRepository(session)
            existing = await seg_repo.list_segments_by_task(task_id)
            if existing:
                status_counts = {}
                for seg in existing:
                    status_counts[seg.status] = status_counts.get(seg.status, 0) + 1
                has_actionable = any(
                    seg.status in (SegmentStatus.PENDING, SegmentStatus.DISPATCHED,
                                   SegmentStatus.TRANSCRIBING, SegmentStatus.SUCCEEDED)
                    for seg in existing
                )
                if has_actionable:
                    logger.info("segments_already_exist", task_id=task_id,
                                count=len(existing), status_counts=status_counts)
                    return
                # All segments are in FAILED state with exhausted retries —
                # the task should fall through to whole-file dispatch instead.
                logger.warning(
                    "segments_all_terminal_failed",
                    task_id=task_id,
                    count=len(existing),
                    status_counts=status_counts,
                    hint="All existing segments are in FAILED state; "
                         "task will be dispatched as whole-file fallback",
                )
                return

        output_dir_path = settings.temp_dir / "segments" / task_id
        tmp_dir = settings.temp_dir / "segments" / f"{task_id}.tmp-{os.getpid()}"
        canonical_path: str | None = None
        try:
            canonical_path = await ensure_canonical_wav(audio_path)
            duration_ms = await get_audio_duration_ms(canonical_path)
            silence_ranges = await silence_detect(canonical_path)

            plan_kwargs: dict[str, int] = {}
            preset = SEGMENT_LEVEL_PRESETS.get(seg_level) if seg_level != "10m" else None
            if preset:
                plan_kwargs["target_duration_ms"] = preset.target_duration_sec * 1000
                plan_kwargs["max_duration_ms"] = preset.max_duration_sec * 1000
                plan_kwargs["search_step_ms"] = preset.search_step_sec * 1000
            plans = plan_segments(duration_ms, silence_ranges, **plan_kwargs)

            if len(plans) <= 1:
                logger.info("segmentation_single_segment", task_id=task_id,
                            duration_ms=duration_ms, hint="below split threshold after planning")
                return

            segment_paths = await split_wav_segments(
                canonical_path, plans, str(tmp_dir), task_id,
            )

            if output_dir_path.exists():
                # Stale directory from a previous attempt whose DB write
                # failed.  Replace it with the fresh split results.
                shutil.rmtree(output_dir_path)
            Path(tmp_dir).rename(output_dir_path)
            final_paths = [
                str(output_dir_path / Path(p).name) for p in segment_paths
            ]

            async with async_session_factory() as session:
                seg_repo = SegmentRepository(session)
                if await seg_repo.list_segments_by_task(task_id):
                    return
                segments = [
                    TaskSegment(
                        segment_id=str(ULID()),
                        task_id=task_id,
                        segment_index=plan.segment_index,
                        source_start_ms=plan.source_start_ms,
                        source_end_ms=plan.source_end_ms,
                        keep_start_ms=plan.keep_start_ms,
                        keep_end_ms=plan.keep_end_ms,
                        storage_path=path,
                        status=SegmentStatus.PENDING,
                    )
                    for plan, path in zip(plans, final_paths)
                ]
                try:
                    await seg_repo.create_segments(segments)
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    existing = await seg_repo.list_segments_by_task(task_id)
                    if existing:
                        logger.info("segments_already_exist_after_race",
                                    task_id=task_id, count=len(existing))
                        return
                    raise

            logger.info("segments_created", task_id=task_id, count=len(plans),
                         duration_ms=duration_ms, silence_ranges=len(silence_ranges))
        except Exception:
            if tmp_dir.exists():
                try:
                    shutil.rmtree(tmp_dir)
                except OSError:
                    pass
            raise

    async def _dispatch_queued_tasks(self) -> None:
        async with self._dispatch_lock:
            await self._dispatch_queued_tasks_locked()

    async def _dispatch_queued_tasks_locked(self) -> None:
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            seg_repo = SegmentRepository(session)

            all_online_stmt = (
                select(ServerInstance)
                .where(ServerInstance.status == "ONLINE")
                .order_by(ServerInstance.server_id.asc())
            )
            all_online = list((await session.execute(all_online_stmt)).scalars().all())
            servers = [s for s in all_online if s.enabled]
            disabled = [s for s in all_online if not s.enabled]
            if disabled:
                logger.warning(
                    "servers_disabled_excluded_from_dispatch",
                    disabled_server_ids=[s.server_id for s in disabled],
                    enabled_count=len(servers),
                    hint="These ONLINE servers have enabled=false and will "
                         "not receive tasks; use `cli server update <id> "
                         "--enabled true` to re-enable",
                )
            if not servers:
                self._clear_plan_pool()
                return

            inflight = await self._get_inflight_snapshot()
            queued_stmt = (
                select(Task)
                .options(selectinload(Task.file))
                .where(Task.status == TaskStatus.QUEUED)
                .order_by(Task.created_at.asc())
                .limit(200)
            )
            queued_tasks = list((await session.execute(queued_stmt)).scalars().all())

            # Also find active segmented parents that still have pending segments
            pending_parent_stmt = (
                select(TaskSegment.task_id)
                .where(TaskSegment.status == SegmentStatus.PENDING)
                .distinct()
            )
            parent_ids_with_pending = set(
                (await session.execute(pending_parent_stmt)).scalars().all()
            )

            queued_task_ids = {t.task_id for t in queued_tasks}
            extra_parent_ids = parent_ids_with_pending - queued_task_ids
            extra_parents: list[Task] = []
            if extra_parent_ids:
                extra_stmt = (
                    select(Task)
                    .options(selectinload(Task.file))
                    .where(
                        Task.task_id.in_(extra_parent_ids),
                        Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
                    )
                )
                extra_parents = list((await session.execute(extra_stmt)).scalars().all())

            all_candidate_tasks = list(queued_tasks) + extra_parents
            if not all_candidate_tasks:
                return

            running_count = await self._count_server_active_work(session)

            server_by_id = {srv.server_id: srv for srv in servers}
            for sid, active_count in running_count.items():
                server = server_by_id.get(sid)
                if server and active_count > server.max_concurrency:
                    logger.error(
                        "slot_overbooked",
                        server_id=sid,
                        active_count=active_count,
                        max_concurrency=server.max_concurrency,
                    )

            server_profiles = [
                ServerProfile(
                    server_id=srv.server_id,
                    host=srv.host,
                    port=srv.port,
                    max_concurrency=srv.max_concurrency,
                    rtf_baseline=srv.rtf_baseline,
                    throughput_rtf=srv.throughput_rtf,
                    penalty_factor=srv.penalty_factor,
                    running_tasks=running_count.get(srv.server_id, 0),
                )
                for srv in servers
            ]

            available_profiles = []
            for sp in server_profiles:
                cb = breaker_registry.get(sp.server_id)
                can_request = getattr(cb, "can_request", cb.allow_request)
                if await can_request():
                    available_profiles.append(sp)
            if not available_profiles:
                return

            # --- Build unified work items (tasks + segments) ---
            regular_tasks: dict[str, Task] = {}
            segment_items: dict[str, tuple[TaskSegment, Task]] = {}
            work_items: list[dict] = []

            for task in all_candidate_tasks:
                if task.task_id in inflight:
                    continue
                if task.status in (TaskStatus.CANCELED.value, TaskStatus.FAILED.value,
                                   TaskStatus.SUCCEEDED.value):
                    continue

                is_segmented = task.task_id in parent_ids_with_pending

                if is_segmented:
                    active = await seg_repo.count_active_segments(task.task_id)
                    max_par = min(
                        len(available_profiles),
                        settings.segment_max_parallel_per_task,
                    )
                    can_dispatch = max_par - active
                    if can_dispatch <= 0:
                        continue
                    pending = await seg_repo.get_pending_segments(
                        task.task_id, limit=can_dispatch,
                    )
                    for seg in pending:
                        if seg.segment_id in inflight:
                            continue
                        seg_dur = seg.duration_ms / 1000.0
                        work_items.append({
                            "task_id": seg.segment_id,
                            "audio_duration_sec": seg_dur,
                            "kind": "segment",
                            "parent_task_id": task.task_id,
                        })
                        segment_items[seg.segment_id] = (seg, task)
                else:
                    if not task.can_transition_to(TaskStatus.DISPATCHED):
                        continue
                    audio_duration = 0.0
                    if task.file and task.file.duration_sec:
                        audio_duration = task.file.duration_sec
                    work_items.append({
                        "task_id": task.task_id,
                        "audio_duration_sec": audio_duration,
                        "kind": "task",
                    })
                    regular_tasks[task.task_id] = task

            if not work_items:
                return

            # --- Two-layer scheduling: Local Refill / Work Steal vs Global Replan ---
            current_available_ids = frozenset(sp.server_id for sp in available_profiles)
            servers_changed = current_available_ids != self._planned_available_server_ids
            new_work = [
                wi for wi in work_items
                if not self._plan_pool.contains(wi["task_id"])
            ]
            has_unplanned = bool(new_work) or not self._plan_pool
            queue_imbalanced = self._check_queue_imbalance(current_available_ids)

            now = time.monotonic()
            cooldown_elapsed = now - self._last_replan_time >= REPLAN_COOLDOWN_SEC

            servers_changed_cooldown_ok = (
                now - self._last_replan_time >= SERVERS_CHANGED_COOLDOWN_SEC
            )

            needs_replan = False
            replan_reason = ""
            if servers_changed and (servers_changed_cooldown_ok
                                    or not self._planned_available_server_ids):
                needs_replan = True
                replan_reason = "servers_changed"
            elif has_unplanned and not self._plan_pool:
                needs_replan = True
                replan_reason = "no_existing_plan"
            elif has_unplanned and cooldown_elapsed:
                needs_replan = True
                replan_reason = "new_work_items"
            elif queue_imbalanced and cooldown_elapsed:
                needs_replan = True
                replan_reason = "queue_imbalance"

            if needs_replan:
                logger.info("global_replan_triggered",
                            reason=replan_reason,
                            since_last_replan_sec=f"{now - self._last_replan_time:.1f}")
                decisions = global_scheduler.schedule_batch(work_items, available_profiles)
                if decisions:
                    self._plan_pool.replace(decisions)
                    self._planned_available_server_ids = current_available_ids
                elif not self._plan_pool:
                    self._planned_available_server_ids = current_available_ids
                else:
                    logger.debug("replan_returned_empty_keeping_existing_plan",
                                 pool_size=len(self._plan_pool))
                    self._planned_available_server_ids = current_available_ids
                self._last_replan_time = now
            elif new_work and not cooldown_elapsed:
                inc_decisions = global_scheduler.schedule_batch(new_work, available_profiles)
                if inc_decisions:
                    tail_finish = self._plan_pool.server_tail_finish()
                    for d in inc_decisions:
                        offset = tail_finish.get(d.server_id, 0.0)
                        if offset > 0:
                            d.estimated_start += offset
                            d.estimated_finish += offset
                    added = self._plan_pool.merge(inc_decisions)
                    logger.debug("incremental_merge",
                                 new_items=len(new_work),
                                 merged=added,
                                 pool_size=len(self._plan_pool))

            if not self._plan_pool:
                return

            work_map: dict[str, object] = {}
            work_map.update(regular_tasks)
            work_map.update({sid: seg for sid, (seg, _) in segment_items.items()})

            stale_ids = self._plan_pool.task_ids - set(work_map.keys())
            if stale_ids:
                for stale_id in stale_ids:
                    self._plan_pool.remove(stale_id)
                logger.debug("plan_pool_stale_purged", count=len(stale_ids))

            max_concurrency_map = {sp.server_id: sp.max_concurrency for sp in available_profiles}
            free_slots = {sp.server_id: max(sp.max_concurrency - sp.running_tasks, 0)
                          for sp in available_profiles}
            profile_map = {sp.server_id: sp for sp in available_profiles}

            to_start_tasks: list[str] = []
            to_start_segments: list[str] = []

            # Phase A: dispatch from PlanPool per-server queues
            for sp in available_profiles:
                sid = sp.server_id
                while free_slots.get(sid, 0) > 0:
                    batch = self._plan_pool.pop_dispatchable(sid, 1)
                    if not batch:
                        break
                    decision = batch[0]

                    if decision.task_id not in work_map or decision.task_id in inflight:
                        continue
                    cb = breaker_registry.get(sid)
                    if not await cb.allow_request():
                        self._plan_pool.merge([decision])
                        logger.warning("dispatch_blocked_by_circuit_breaker", server_id=sid)
                        break

                    if decision.kind == "segment":
                        seg_tuple = segment_items.get(decision.task_id)
                        if seg_tuple is None:
                            continue
                        seg, parent_task = seg_tuple
                        seg.run_generation += 1
                        await seg_repo.update_segment_status(
                            seg, SegmentStatus.DISPATCHED, server_id=sid,
                        )
                        await session.refresh(parent_task, ["status"])
                        if parent_task.status == TaskStatus.QUEUED.value:
                            if parent_task.can_transition_to(TaskStatus.DISPATCHED):
                                parent_task.assigned_server_id = sid
                                await repo.update_task_status(parent_task, TaskStatus.DISPATCHED)
                        elif not parent_task.assigned_server_id:
                            parent_task.assigned_server_id = sid
                        to_start_segments.append(seg.segment_id)
                    else:
                        task = regular_tasks.get(decision.task_id)
                        if (task is None or task.task_id in inflight
                                or not task.can_transition_to(TaskStatus.DISPATCHED)):
                            continue
                        task.run_generation += 1
                        task.assigned_server_id = sid
                        task.eta_seconds = int(decision.estimated_duration)
                        await repo.update_task_status(task, TaskStatus.DISPATCHED)
                        to_start_tasks.append(task.task_id)

                    free_slots[sid] -= 1

            # Phase B: work stealing — idle servers steal from busy server tails
            for sp in available_profiles:
                sid = sp.server_id
                skipped_steal_ids: set[str] = set()
                while free_slots.get(sid, 0) > 0:
                    result = self._find_steal_candidate(
                        sp, profile_map, work_map, inflight,
                        excluded_task_ids=skipped_steal_ids,
                    )
                    if result is None:
                        break
                    decision, source_server, est_stolen, source_remaining, estimated_gain = result

                    cb = breaker_registry.get(sid)
                    if not await cb.allow_request():
                        logger.warning("work_steal_blocked_by_circuit_breaker", server_id=sid)
                        break

                    if decision.kind == "segment":
                        seg_tuple = segment_items.get(decision.task_id)
                        if seg_tuple is None or decision.task_id in inflight:
                            self._plan_pool.remove(decision.task_id)
                            continue
                        seg, parent_task = seg_tuple
                        active_count = await seg_repo.count_active_segments(parent_task.task_id)
                        steal_max = min(len(available_profiles), settings.segment_max_parallel_per_task)
                        if active_count >= steal_max:
                            skipped_steal_ids.add(decision.task_id)
                            continue
                        seg.run_generation += 1
                        await seg_repo.update_segment_status(
                            seg, SegmentStatus.DISPATCHED, server_id=sid,
                        )
                        await session.refresh(parent_task, ["status"])
                        if parent_task.status == TaskStatus.QUEUED.value:
                            if parent_task.can_transition_to(TaskStatus.DISPATCHED):
                                parent_task.assigned_server_id = sid
                                await repo.update_task_status(parent_task, TaskStatus.DISPATCHED)
                        elif not parent_task.assigned_server_id:
                            parent_task.assigned_server_id = sid
                        to_start_segments.append(seg.segment_id)
                    else:
                        task = regular_tasks.get(decision.task_id)
                        if (task is None or task.task_id in inflight
                                or not task.can_transition_to(TaskStatus.DISPATCHED)):
                            self._plan_pool.remove(decision.task_id)
                            continue
                        task.run_generation += 1
                        task.assigned_server_id = sid
                        task.eta_seconds = int(est_stolen)
                        await repo.update_task_status(task, TaskStatus.DISPATCHED)
                        to_start_tasks.append(task.task_id)

                    self._plan_pool.remove(decision.task_id)
                    target_free_slots_before = free_slots.get(sid, 0)
                    free_slots[sid] -= 1
                    event_task_id = decision.parent_task_id or decision.task_id
                    session.add(TaskEvent(
                        event_id=str(ULID()),
                        task_id=event_task_id,
                        from_status=None,
                        to_status=TaskStatus.DISPATCHED.value,
                        payload_json=json.dumps({
                            "event_type": "work_steal",
                            "work_id": decision.task_id,
                            "kind": decision.kind,
                            "parent_task_id": decision.parent_task_id,
                            "from_server": source_server,
                            "to_server": sid,
                            "target_free_slots_before": target_free_slots_before,
                            "source_remaining_before_sec": round(source_remaining, 1),
                            "est_original_sec": round(decision.estimated_duration, 1),
                            "est_stolen_sec": round(est_stolen, 1),
                            "estimated_gain_sec": round(estimated_gain, 1),
                            "reason": "idle_slot_positive_gain",
                        }, ensure_ascii=False),
                    ))
                    logger.info("work_steal",
                                work_id=decision.task_id,
                                kind=decision.kind,
                                parent_task_id=decision.parent_task_id,
                                from_server=source_server,
                                to_server=sid,
                                target_free_slots_before=target_free_slots_before,
                                source_remaining_before_sec=f"{source_remaining:.1f}",
                                est_original=f"{decision.estimated_duration:.1f}s",
                                est_stolen=f"{est_stolen:.1f}s",
                                estimated_gain_sec=f"{estimated_gain:.1f}",
                                reason="idle_slot_positive_gain")

            if not to_start_tasks and not to_start_segments:
                return

            # Post-dispatch invariant: verify no server exceeds max_concurrency
            post_running = await self._count_server_active_work(session)

            overcommitted: dict[str, tuple[int, int]] = {}
            for sid, count in post_running.items():
                limit = max_concurrency_map.get(sid)
                if limit is not None and count > limit:
                    overcommitted[sid] = (count, limit)

            if overcommitted:
                await session.rollback()
                self._clear_plan_pool(reset_server_ids=False)
                for sid, (count, limit) in overcommitted.items():
                    logger.error(
                        "slot_overcommit_dispatch_blocked",
                        server_id=sid,
                        active_count=count,
                        max_concurrency=limit,
                        dispatched_tasks=len(to_start_tasks),
                        dispatched_segments=len(to_start_segments),
                    )
                return

            await session.commit()

        for task_id in to_start_tasks:
            await self._mark_inflight(task_id)
            asyncio.create_task(self._execute_task(task_id), name=f"asr-task-{task_id}")

        for segment_id in to_start_segments:
            await self._mark_inflight(segment_id)
            asyncio.create_task(self._execute_segment(segment_id), name=f"asr-seg-{segment_id}")

    async def _count_server_active_work(self, session) -> dict[str, int]:
        """Count real server slot usage.

        Whole-file tasks occupy slots. Segmented parent tasks are logical
        containers, so only their active TaskSegment rows occupy slots.
        """
        active_segmented_task_ids = (
            select(TaskSegment.task_id).distinct()
            .where(TaskSegment.status.in_([
                SegmentStatus.PENDING,
                SegmentStatus.DISPATCHED,
                SegmentStatus.TRANSCRIBING,
                SegmentStatus.SUCCEEDED,
            ]))
        )

        whole_task_stmt = (
            select(Task.assigned_server_id, func.count())
            .where(
                Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
                Task.assigned_server_id.is_not(None),
                Task.task_id.not_in(active_segmented_task_ids),
            )
            .group_by(Task.assigned_server_id)
        )
        counts: dict[str, int] = {
            sid: count for sid, count in (await session.execute(whole_task_stmt)).all()
            if sid
        }

        segment_stmt = (
            select(TaskSegment.assigned_server_id, func.count())
            .where(
                TaskSegment.status.in_([
                    SegmentStatus.DISPATCHED,
                    SegmentStatus.TRANSCRIBING,
                ]),
                TaskSegment.assigned_server_id.is_not(None),
            )
            .group_by(TaskSegment.assigned_server_id)
        )
        for sid, count in (await session.execute(segment_stmt)).all():
            if sid:
                counts[sid] = counts.get(sid, 0) + count

        return counts

    async def _execute_task(self, task_id: str) -> None:
        server = None
        try:
            dispatch_info = await self._load_dispatch_info(task_id)
            if dispatch_info is None:
                return
            task, server, file_record = dispatch_info
            expected_generation = task.run_generation

            if not task.can_transition_to(TaskStatus.TRANSCRIBING):
                return

            task_started_at = None
            async with async_session_factory() as session:
                repo = TaskRepository(session)
                db_task = await repo.get_task(task_id)
                if db_task is None:
                    return
                if db_task.can_transition_to(TaskStatus.TRANSCRIBING):
                    task_started_at = datetime.now(timezone.utc)
                    db_task.started_at = task_started_at
                    await repo.update_task_status(db_task, TaskStatus.TRANSCRIBING)
                    await session.commit()

            audio_path = file_record.storage_path
            if needs_conversion(audio_path):
                logger.info("audio_needs_conversion", task_id=task_id, path=audio_path)
                try:
                    audio_path = await ensure_wav(audio_path)
                except RuntimeError as conv_err:
                    if settings.preprocess_fallback_enabled:
                        logger.warning(
                            "audio_preprocessing_skipped",
                            task_id=task_id,
                            reason=str(conv_err),
                            fallback="sending original file with wav_format=others",
                        )
                    else:
                        await self._mark_task_failed(task_id, f"Audio preprocessing failed: {conv_err}")
                        return

            profile = self._build_message_profile(task, audio_path)
            adapter = get_adapter(
                protocol_version=server.protocol_version,
                server_type=self._normalize_server_type(server),
            )
            result = await self._transcribe_with_protocol_fallback(
                adapter=adapter,
                server=server,
                audio_path=audio_path,
                profile=profile,
                audio_duration_sec=file_record.duration_sec,
            )

            if result.error:
                await breaker_registry.get(server.server_id).record_failure()
                await self._mark_task_failed(task_id, result.error)
                return

            audio_duration = 0.0
            if file_record and hasattr(file_record, "duration_sec") and file_record.duration_sec:
                audio_duration = file_record.duration_sec

            if not result.text or not result.text.strip():
                logger.warning(
                    "asr_empty_text",
                    task_id=task_id,
                    server_id=server.server_id,
                    duration_sec=audio_duration,
                    hint="Silent audio or no speech detected",
                )
                if self._should_retry_empty_result(audio_duration):
                    await breaker_registry.get(server.server_id).record_failure()
                    await self._mark_task_failed(
                        task_id,
                        f"Empty ASR result for {audio_duration:.1f}s audio",
                        error_code="EMPTY_RESULT",
                    )
                    return
                await breaker_registry.get(server.server_id).record_success()
                result.text = ""
            else:
                await breaker_registry.get(server.server_id).record_success()

            if audio_duration > 0 and task_started_at:
                actual_sec = (datetime.now(timezone.utc) - task_started_at).total_seconds()
                global_scheduler.calibrate_after_completion(
                    server_id=server.server_id,
                    audio_duration_sec=audio_duration,
                    actual_duration_sec=actual_sec,
                    predicted_duration_sec=float(task.eta_seconds) if task.eta_seconds is not None else None,
                    work_kind="task",
                )

            raw = result.raw if isinstance(result.raw, dict) and result.raw else {}
            if "text" not in raw:
                raw["text"] = result.text
            if "mode" not in raw:
                raw["mode"] = result.mode

            if await self._is_stale_run(task_id, expected_generation):
                logger.warning("stale_task_result_discarded",
                               task_id=task_id,
                               expected_gen=expected_generation)
                return

            await save_result(task_id, to_json(raw), "json")
            await save_result(task_id, to_txt(raw), "txt")
            await save_result(task_id, to_srt(raw), "srt")
            await self._mark_task_succeeded(task_id)
        except Exception as e:
            logger.exception("task_execute_unhandled_error", task_id=task_id, error=str(e))
            if server is not None:
                await breaker_registry.get(server.server_id).record_failure()
            await self._mark_task_failed(task_id, str(e))
        finally:
            await self._unmark_inflight(task_id)

    async def _load_dispatch_info(self, task_id: str) -> tuple[Task, ServerInstance, File] | None:
        async with async_session_factory() as session:
            task_stmt = select(Task).where(Task.task_id == task_id)
            task = (await session.execute(task_stmt)).scalar_one_or_none()
            if task is None or not task.assigned_server_id:
                return None

            server_stmt = select(ServerInstance).where(ServerInstance.server_id == task.assigned_server_id)
            server = (await session.execute(server_stmt)).scalar_one_or_none()
            if server is None:
                logger.warning("dispatch_server_missing", task_id=task_id,
                               server_id=task.assigned_server_id)
                await self._mark_task_failed(
                    task_id,
                    f"Assigned server {task.assigned_server_id} no longer exists",
                )
                return None

            file_stmt = select(File).where(File.file_id == task.file_id)
            file_record = (await session.execute(file_stmt)).scalar_one_or_none()
            if file_record is None:
                logger.warning("dispatch_file_missing", task_id=task_id,
                               file_id=task.file_id)
                await self._mark_task_failed(task_id, f"File {task.file_id} not found")
                return None

            return task, server, file_record

    async def _mark_task_succeeded(self, task_id: str) -> None:
        pending_delivery = None
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task is None:
                return
            task.result_path = task_id
            task.error_code = None
            task.error_message = None
            if task.can_transition_to(TaskStatus.SUCCEEDED):
                event = await repo.update_task_status(task, TaskStatus.SUCCEEDED)
                outbox = await self._callback_worker.enqueue(session, task, event.event_id, TaskStatus.SUCCEEDED)
                if outbox is not None:
                    pending_delivery = (outbox.outbox_id, task.callback_secret)
                logger.info("task_transcription_succeeded", task_id=task_id)
            elif task.status in (
                TaskStatus.QUEUED.value,
                TaskStatus.DISPATCHED.value,
                TaskStatus.TRANSCRIBING.value,
            ):
                from ulid import ULID

                from_status = task.status
                task.status = TaskStatus.SUCCEEDED.value
                task.progress = 1.0
                task.assigned_server_id = None
                task.completed_at = datetime.now(timezone.utc)
                event = TaskEvent(
                    event_id=str(ULID()),
                    task_id=task.task_id,
                    from_status=from_status,
                    to_status=TaskStatus.SUCCEEDED.value,
                    payload_json='{"recovered_from_late_completion": true}',
                )
                session.add(event)
                await session.flush()
                outbox = await self._callback_worker.enqueue(session, task, event.event_id, TaskStatus.SUCCEEDED)
                if outbox is not None:
                    pending_delivery = (outbox.outbox_id, task.callback_secret)
                logger.warning(
                    "task_succeeded_after_status_recovery",
                    task_id=task_id,
                    from_status=from_status,
                    hint="Transcription result was already saved; task state was "
                         "reconciled to SUCCEEDED after a stale reset/requeue.",
                )
            else:
                logger.warning(
                    "task_succeeded_but_transition_blocked",
                    task_id=task_id,
                    current_status=task.status,
                    hint="Status was likely reset by a concurrent restart; "
                         "transcription completed but state cannot advance to SUCCEEDED",
                )
            user_id = task.user_id
            await session.commit()
        await rate_limiter.record_task_completed(user_id)
        if not self._plan_pool:
            self._clear_plan_pool(reset_server_ids=False)
        self._request_dispatch()
        if pending_delivery:
            await self._callback_worker.try_deliver(*pending_delivery)

    async def _mark_task_failed(
        self,
        task_id: str,
        message: str,
        *,
        error_code: str = "TRANSCRIBE_ERROR",
    ) -> None:
        pending_delivery = None
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task is None:
                return
            task.error_code = error_code
            task.error_message = message[:2000]
            is_terminal = task.retry_count >= settings.max_retry_count
            if task.can_transition_to(TaskStatus.FAILED):
                event = await repo.update_task_status(task, TaskStatus.FAILED)
                if is_terminal:
                    outbox = await self._callback_worker.enqueue(
                        session,
                        task,
                        event.event_id,
                        TaskStatus.FAILED,
                        error_message=message[:2000],
                    )
                    if outbox is not None:
                        pending_delivery = (outbox.outbox_id, task.callback_secret)
            user_id = task.user_id
            await session.commit()
            logger.warning("task_transcription_failed", task_id=task_id,
                           error=message[:300], terminal=is_terminal,
                           retry_count=task.retry_count)
        if is_terminal:
            await rate_limiter.record_task_completed(user_id)
        if not self._plan_pool:
            self._clear_plan_pool(reset_server_ids=False)
        self._request_dispatch()
        if pending_delivery:
            await self._callback_worker.try_deliver(*pending_delivery)

    # ------------------------------------------------------------------
    # Segment-level execution (Stage 7)
    # ------------------------------------------------------------------

    async def _execute_segment(self, segment_id: str) -> None:
        """Execute a single segment transcription job."""
        server = None
        try:
            info = await self._load_segment_dispatch_info(segment_id)
            if info is None:
                return
            segment, parent_task, server = info
            expected_seg_gen = segment.run_generation

            async with async_session_factory() as session:
                repo = TaskRepository(session)
                fresh_parent = await repo.get_task(parent_task.task_id)
                parent_status = fresh_parent.status if fresh_parent else parent_task.status

            if parent_status in (TaskStatus.CANCELED.value, TaskStatus.FAILED.value):
                logger.info("segment_skipped_parent_terminal",
                            segment_id=segment_id, parent_status=parent_status)
                async with async_session_factory() as session:
                    seg_repo = SegmentRepository(session)
                    seg = await seg_repo.get_segment(segment_id)
                    if seg and seg.status in (
                        SegmentStatus.DISPATCHED, SegmentStatus.PENDING,
                    ):
                        await seg_repo.update_segment_status(
                            seg, SegmentStatus.FAILED,
                            error_message=f"Parent task {parent_status}",
                        )
                        await session.commit()
                return

            segment_started_at: datetime | None = None
            async with async_session_factory() as session:
                seg_repo = SegmentRepository(session)
                repo = TaskRepository(session)
                seg = await seg_repo.get_segment(segment_id)
                if seg is None:
                    return
                await seg_repo.update_segment_status(seg, SegmentStatus.TRANSCRIBING)
                segment_started_at = datetime.now(timezone.utc)

                db_task = await repo.get_task(seg.task_id)
                if db_task is not None:
                    if db_task.status == TaskStatus.DISPATCHED.value:
                        if db_task.can_transition_to(TaskStatus.TRANSCRIBING):
                            db_task.started_at = segment_started_at
                            await repo.update_task_status(db_task, TaskStatus.TRANSCRIBING)
                    elif db_task.status == TaskStatus.QUEUED.value:
                        if db_task.can_transition_to(TaskStatus.DISPATCHED):
                            await repo.update_task_status(db_task, TaskStatus.DISPATCHED)
                        if db_task.can_transition_to(TaskStatus.TRANSCRIBING):
                            db_task.started_at = segment_started_at
                            await repo.update_task_status(db_task, TaskStatus.TRANSCRIBING)
                await session.commit()

            async with async_session_factory() as session:
                fresh_task = await TaskRepository(session).get_task(parent_task.task_id)
                if fresh_task and fresh_task.status in (
                    TaskStatus.CANCELED.value, TaskStatus.FAILED.value,
                ):
                    seg_repo2 = SegmentRepository(session)
                    seg2 = await seg_repo2.get_segment(segment_id)
                    if seg2 and seg2.status not in (
                        SegmentStatus.SUCCEEDED, SegmentStatus.FAILED,
                    ):
                        await seg_repo2.update_segment_status(
                            seg2, SegmentStatus.FAILED,
                            error_message=f"Parent task {fresh_task.status} before transcription",
                        )
                    await session.commit()
                    logger.info("segment_aborted_pre_transcription",
                                segment_id=segment_id, parent_status=fresh_task.status)
                    return

            audio_path = segment.storage_path
            profile = self._build_message_profile(parent_task, audio_path)
            adapter = get_adapter(
                protocol_version=server.protocol_version,
                server_type=self._normalize_server_type(server),
            )
            result = await self._transcribe_with_protocol_fallback(
                adapter=adapter,
                server=server,
                audio_path=audio_path,
                profile=profile,
                audio_duration_sec=segment.duration_ms / 1000.0,
            )

            if result.error:
                await breaker_registry.get(server.server_id).record_failure()
                await self._mark_segment_failed(segment_id, result.error)
                return

            seg_audio_duration = segment.duration_ms / 1000.0

            if not result.text or not result.text.strip():
                if self._should_retry_empty_result(seg_audio_duration, is_segment=True):
                    await breaker_registry.get(server.server_id).record_failure()
                    await self._mark_segment_failed(
                        segment_id,
                        f"Empty ASR result for {seg_audio_duration:.1f}s segment",
                    )
                    return
                await breaker_registry.get(server.server_id).record_success()
                result.text = ""
            else:
                await breaker_registry.get(server.server_id).record_success()

            if seg_audio_duration > 0 and segment_started_at:
                actual_sec = (datetime.now(timezone.utc) - segment_started_at).total_seconds()
                server_profile = ServerProfile(
                    server_id=server.server_id,
                    host=server.host,
                    port=server.port,
                    max_concurrency=server.max_concurrency,
                    rtf_baseline=server.rtf_baseline,
                    throughput_rtf=server.throughput_rtf,
                    penalty_factor=server.penalty_factor,
                )
                predicted_sec = global_scheduler.estimate_processing_time(
                    seg_audio_duration,
                    server_profile,
                    work_kind="segment",
                )
                global_scheduler.calibrate_after_completion(
                    server_id=server.server_id,
                    audio_duration_sec=seg_audio_duration,
                    actual_duration_sec=actual_sec,
                    predicted_duration_sec=predicted_sec if predicted_sec > 0 else None,
                    work_kind="segment",
                )

            if await self._is_stale_segment_run(segment_id, expected_seg_gen):
                logger.warning("stale_segment_result_discarded",
                               segment_id=segment_id,
                               expected_gen=expected_seg_gen)
                return

            raw = result.raw if isinstance(result.raw, dict) and result.raw else {}
            if "text" not in raw:
                raw["text"] = result.text
            if "mode" not in raw:
                raw["mode"] = result.mode
            raw_json = json.dumps(raw, ensure_ascii=False)

            await self._mark_segment_succeeded(segment_id, raw_json)

        except Exception as e:
            logger.exception("segment_execute_error", segment_id=segment_id, error=str(e))
            if server is not None:
                await breaker_registry.get(server.server_id).record_failure()
            await self._mark_segment_failed(segment_id, str(e))
        finally:
            await self._unmark_inflight(segment_id)

    @staticmethod
    def _should_retry_empty_result(
        audio_duration_sec: float | None,
        *,
        is_segment: bool = False,
    ) -> bool:
        threshold = (
            EMPTY_RESULT_RETRY_MIN_SEGMENT_SEC if is_segment
            else EMPTY_RESULT_RETRY_MIN_AUDIO_SEC
        )
        return (audio_duration_sec or 0.0) >= threshold

    async def _load_segment_dispatch_info(
        self, segment_id: str,
    ) -> tuple[TaskSegment, Task, ServerInstance] | None:
        async with async_session_factory() as session:
            seg = (await session.execute(
                select(TaskSegment).where(TaskSegment.segment_id == segment_id)
            )).scalar_one_or_none()
            if seg is None or not seg.assigned_server_id:
                logger.warning("segment_dispatch_info_missing", segment_id=segment_id)
                return None

            task = (await session.execute(
                select(Task).options(selectinload(Task.file))
                .where(Task.task_id == seg.task_id)
            )).scalar_one_or_none()
            if task is None:
                logger.warning("segment_parent_task_missing",
                               segment_id=segment_id, task_id=seg.task_id)
                return None

            server = (await session.execute(
                select(ServerInstance)
                .where(ServerInstance.server_id == seg.assigned_server_id)
            )).scalar_one_or_none()
            if server is None:
                await self._mark_segment_failed(
                    segment_id,
                    f"Assigned server {seg.assigned_server_id} not found",
                )
                return None

            return seg, task, server

    async def _mark_segment_succeeded(self, segment_id: str, raw_result_json: str) -> None:
        task_id: str | None = None
        async with async_session_factory() as session:
            seg_repo = SegmentRepository(session)
            segment = await seg_repo.get_segment(segment_id)
            if segment is None:
                return
            task_id = segment.task_id
            if segment.status not in (
                SegmentStatus.DISPATCHED.value,
                SegmentStatus.TRANSCRIBING.value,
            ):
                logger.warning(
                    "segment_success_ignored_non_active",
                    segment_id=segment_id,
                    status=segment.status,
                )
                return

            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task is None or task.status in (
                TaskStatus.CANCELED.value,
                TaskStatus.FAILED.value,
                TaskStatus.SUCCEEDED.value,
            ):
                logger.warning(
                    "segment_success_ignored_parent_terminal",
                    segment_id=segment_id,
                    task_id=task_id,
                    parent_status=task.status if task else None,
                )
                return

            await seg_repo.update_segment_status(
                segment, SegmentStatus.SUCCEEDED, raw_result_json=raw_result_json,
            )

            total_keep = await seg_repo.total_keep_duration_ms(task_id)
            completed_keep = await seg_repo.sum_completed_duration_ms(task_id)

            if task and total_keep > 0:
                progress = 0.20 + 0.75 * (completed_keep / total_keep)
                task.progress = min(progress, 0.95)

            await session.commit()

        logger.info("segment_transcription_succeeded",
                     segment_id=segment_id, task_id=task_id,
                     completed_keep_ms=completed_keep, total_keep_ms=total_keep)

        if task_id:
            await self._finalizer.maybe_finalize(task_id)

        self._request_dispatch()

    async def _mark_segment_failed(self, segment_id: str, message: str) -> None:
        pending_delivery = None
        parent_failed = False
        user_id: str | None = None

        async with async_session_factory() as session:
            seg_repo = SegmentRepository(session)
            segment = await seg_repo.get_segment(segment_id)
            if segment is None:
                return
            repo = TaskRepository(session)
            task = await repo.get_task(segment.task_id)
            if segment.status not in (
                SegmentStatus.DISPATCHED.value,
                SegmentStatus.TRANSCRIBING.value,
            ):
                logger.warning(
                    "segment_failure_ignored_non_active",
                    segment_id=segment_id,
                    status=segment.status,
                )
                return
            if task is None or task.status in (
                TaskStatus.CANCELED.value,
                TaskStatus.FAILED.value,
                TaskStatus.SUCCEEDED.value,
            ):
                logger.warning(
                    "segment_failure_ignored_parent_terminal",
                    segment_id=segment_id,
                    task_id=segment.task_id,
                    parent_status=task.status if task else None,
                )
                return

            if segment.retry_count < settings.segment_max_retry_count:
                segment.error_message = message[:2000]
                await seg_repo.increment_retry(segment)
                await session.commit()
                logger.info("segment_retry_queued",
                            segment_id=segment_id,
                            task_id=segment.task_id,
                            retry=segment.retry_count)
            else:
                if task and task.can_transition_to(TaskStatus.FAILED):
                    task.error_code = "SEGMENT_RETRY_EXHAUSTED"
                    error_msg = (
                        f"Segment {segment.segment_index} failed after "
                        f"{segment.retry_count} retries: {message[:500]}"
                    )
                    task.error_message = error_msg
                    event = await repo.update_task_status(task, TaskStatus.FAILED)
                    if task.retry_count >= settings.max_retry_count:
                        outbox = await self._callback_worker.enqueue(
                            session, task, event.event_id, TaskStatus.FAILED,
                            error_message=error_msg,
                        )
                        if outbox is not None:
                            pending_delivery = (outbox.outbox_id, task.callback_secret)
                        user_id = task.user_id
                        parent_failed = True
                    orphan_count = await self._fail_orphan_segments(
                        seg_repo, segment.task_id, exclude_id=segment_id,
                    )
                    if orphan_count > 0:
                        logger.info("orphan_segments_failed_with_parent",
                                    task_id=segment.task_id,
                                    count=orphan_count)
                await session.commit()
                logger.warning("segment_retry_exhausted",
                               segment_id=segment_id,
                               task_id=segment.task_id,
                               segment_index=segment.segment_index,
                               retry_count=segment.retry_count)

        if parent_failed and user_id:
            await rate_limiter.record_task_completed(user_id)
        if not self._plan_pool:
            self._clear_plan_pool(reset_server_ids=False)
        self._request_dispatch()
        if pending_delivery:
            await self._callback_worker.try_deliver(*pending_delivery)

    async def _is_stale_run(self, task_id: str, expected_generation: int) -> bool:
        """Return True if the task's run_generation has advanced past expected."""
        async with async_session_factory() as session:
            task = await TaskRepository(session).get_task(task_id)
            if task is None:
                return True
            return task.run_generation != expected_generation

    async def _is_stale_segment_run(self, segment_id: str, expected_generation: int) -> bool:
        """Return True if the segment's run_generation has advanced past expected."""
        async with async_session_factory() as session:
            seg = await SegmentRepository(session).get_segment(segment_id)
            if seg is None:
                return True
            return seg.run_generation != expected_generation

    @staticmethod
    async def _fail_orphan_segments(
        seg_repo: SegmentRepository,
        task_id: str,
        *,
        exclude_id: str | None = None,
    ) -> int:
        """Mark all non-terminal segments of a failed parent as FAILED.

        Prevents orphan PENDING/DISPATCHED/TRANSCRIBING segments from
        lingering in the database after the parent task has been declared
        failed, which would otherwise pollute dispatch queries.
        """
        from datetime import datetime, timezone as _tz
        segments = await seg_repo.list_segments_by_task(task_id)
        terminal = (SegmentStatus.SUCCEEDED, SegmentStatus.FAILED)
        count = 0
        for seg in segments:
            if seg.segment_id == exclude_id:
                continue
            if seg.status in (s.value for s in terminal):
                continue
            seg.status = SegmentStatus.FAILED.value
            seg.error_message = "Parent task failed"
            seg.completed_at = datetime.now(_tz.utc)
            count += 1
        if count > 0:
            await seg_repo._session.flush()
        return count

    # ------------------------------------------------------------------
    # Transcription helpers
    # ------------------------------------------------------------------

    async def _transcribe_with_protocol_fallback(
        self,
        *,
        adapter,
        server: ServerInstance,
        audio_path: str,
        profile: MessageProfile,
        audio_duration_sec: float | None = None,
    ):
        timeout = float(settings.task_timeout_seconds)
        if audio_duration_sec and audio_duration_sec > 0:
            dynamic_timeout = max(
                float(settings.segment_timeout_min_seconds),
                float(audio_duration_sec) * float(settings.segment_timeout_audio_multiplier),
            )
            timeout = min(timeout, dynamic_timeout)

        result = await adapter.transcribe(
            host=server.host,
            port=server.port,
            audio_path=audio_path,
            profile=profile,
            use_ssl=True,
            ssl_verify=False,
            timeout=timeout,
        )
        if not result.error:
            return result

        msg = (result.error or "").lower()
        is_ssl_error = any(k in msg for k in ("ssl", "tls", "certificate"))
        is_ws_error = "websocket" in msg or "http response" in msg
        if not (is_ssl_error or is_ws_error):
            return result

        logger.warning(
            "transcribe_wss_failed_retry_plain_ws",
            server_id=server.server_id,
            host=server.host,
            port=server.port,
            error=result.error,
        )
        ws_result = await adapter.transcribe(
            host=server.host,
            port=server.port,
            audio_path=audio_path,
            profile=profile,
            use_ssl=False,
            timeout=timeout,
        )
        return ws_result if not ws_result.error else result

    def _build_message_profile(self, task: Task, audio_path: str) -> MessageProfile:
        """Build MessageProfile with proper wav_name and format detection."""
        from pathlib import Path as _Path

        wav_name = "audio"
        if task.file and task.file.original_name:
            wav_name = _Path(task.file.original_name).stem
        elif audio_path:
            wav_name = _Path(audio_path).stem

        ext = _Path(audio_path).suffix.lower() if audio_path else ""
        if ext in (".wav", ".pcm"):
            wav_format = "pcm"
        else:
            wav_format = "others"

        profile = MessageProfile(
            wav_name=wav_name,
            wav_format=wav_format,
        )

        if task.language:
            profile.svs_lang = task.language

        if task.options_json:
            try:
                options = json.loads(task.options_json)
                hotwords = options.get("hotwords")
                if hotwords:
                    profile.hotwords = str(hotwords)
                mode = options.get("mode")
                if mode:
                    from app.adapters.base import RecognitionMode
                    try:
                        profile.mode = RecognitionMode(mode)
                    except ValueError:
                        pass
                if "use_itn" in options:
                    profile.use_itn = bool(options["use_itn"])
                if "use_punc" in options:
                    profile.use_punc = bool(options["use_punc"])
                if "use_spk" in options:
                    profile.use_spk = bool(options["use_spk"])
            except Exception:
                logger.warning("task_options_parse_failed", task_id=task.task_id)
        return profile

    @staticmethod
    def _normalize_server_type(server: ServerInstance) -> str | None:
        st = (server.server_type or "").strip().lower()
        if st:
            return st
        pv = (server.protocol_version or "").strip().lower().replace("-", "_")
        if "main" in pv:
            return "funasr_main"
        if "legacy" in pv or "old" in pv:
            return "legacy"
        return None

    def _find_steal_candidate(
        self,
        idle_profile: ServerProfile,
        profile_map: dict[str, ServerProfile],
        work_map: dict[str, object],
        inflight: set[str],
        max_candidates_per_queue: int = 3,
        excluded_task_ids: set[str] | None = None,
    ) -> tuple[ScheduleDecision, str, float, float, float] | None:
        """Find the best work item to steal for an idle server.

        Scans PlanPool tails of other servers, checking up to
        max_candidates_per_queue items per server to find the candidate
        with the greatest improvement.
        Returns (decision, source_server_id, est_processing_time_on_idle,
        source_remaining_time, estimated_gain) or None.
        """
        best: tuple[ScheduleDecision, str, float, float, float] | None = None
        best_improvement = 0.0
        excluded_task_ids = excluded_task_ids or set()

        for source_sid in list(self._plan_pool.server_ids):
            if source_sid == idle_profile.server_id:
                continue
            source_profile = profile_map.get(source_sid)
            if source_profile is None:
                continue
            q = self._plan_pool.get_queue_snapshot(source_sid)
            if not q:
                continue
            checked = 0
            for idx_from_end, decision in enumerate(reversed(q)):
                if checked >= max_candidates_per_queue:
                    break
                if decision.task_id in excluded_task_ids:
                    continue
                if decision.task_id in inflight:
                    continue
                if work_map.get(decision.task_id) is None:
                    continue
                checked += 1
                est_stolen = global_scheduler.estimate_processing_time(
                    decision.audio_duration_sec,
                    idle_profile,
                    work_kind=decision.kind,
                )
                decision_idx = len(q) - 1 - idx_from_end
                duration_based = (
                    sum(d.estimated_duration for d in q[:decision_idx])
                    + decision.estimated_duration
                )
                source_remaining = max(duration_based, decision.estimated_finish)
                improvement = source_remaining - est_stolen
                if decision.kind == "segment":
                    min_gain = max(2.0, decision.estimated_duration * 0.05)
                else:
                    min_gain = max(3.0, decision.estimated_duration * 0.05)
                if improvement > min_gain and improvement > best_improvement:
                    best = (decision, source_sid, est_stolen, source_remaining, improvement)
                    best_improvement = improvement
        return best

    def _check_queue_imbalance(self, available_server_ids: frozenset[str]) -> bool:
        """Detect true workload imbalance across PlanPool queues.

        Returns True only for genuine imbalance that warrants a global replan.
        Distinguishes 'true_imbalance' from 'structural_queue_empty'.
        """
        if not self._plan_pool:
            return False

        server_remaining = self._plan_pool.server_remaining_sec()
        if not server_remaining or not any(v > 0.0 for v in server_remaining.values()):
            return False

        exhausted_ids = (
            self._planned_available_server_ids - set(server_remaining.keys())
        ) & available_server_ids
        if exhausted_ids:
            total_remaining = sum(server_remaining.values())
            if total_remaining < 30.0:
                logger.debug("structural_queue_empty",
                             exhausted=list(exhausted_ids),
                             remaining_sec=f"{total_remaining:.1f}",
                             hint="low backlog, work steal preferred over replan")
                return False
            logger.info("true_imbalance_idle_server",
                        exhausted=list(exhausted_ids),
                        remaining={sid: f"{v:.1f}s" for sid, v in server_remaining.items()})
            return True

        positives = [v for v in server_remaining.values() if v > 0]
        if len(positives) >= 2:
            ratio = max(positives) / min(positives)
            if ratio > REPLAN_IMBALANCE_RATIO:
                logger.info("true_imbalance_ratio",
                            ratio=f"{ratio:.2f}",
                            threshold=f"{REPLAN_IMBALANCE_RATIO:.1f}",
                            remaining={sid: f"{v:.1f}s" for sid, v in server_remaining.items()})
                return True

        return False

    def _clear_plan_pool(self, *, reset_server_ids: bool = True) -> None:
        self._plan_pool.clear()
        if reset_server_ids:
            self._planned_available_server_ids = frozenset()

    @staticmethod
    def _parse_segment_level(options_json: str | None) -> str:
        """Extract segment_level preference from task options_json.

        Returns ``"off"``, ``"10m"`` (default), ``"20m"``, or ``"30m"``.

        Backward compatibility: tasks persisted before the parameter merge
        may contain ``auto_segment`` instead of ``segment_level``.  The
        legacy values are mapped as follows:

        - ``auto_segment=off``  → ``"off"``
        - ``auto_segment=on``   → ``segment_level`` value (or ``"10m"``)
        - ``auto_segment=auto`` → ``segment_level`` value (or ``"10m"``)
        """
        if not options_json:
            return "10m"
        try:
            opts = json.loads(options_json)

            legacy = opts.get("auto_segment")
            if legacy == "off":
                return "off"

            value = opts.get("segment_level", "10m")
            if value == "off":
                return "off"
            return value if value in SEGMENT_LEVEL_PRESETS else "10m"
        except (json.JSONDecodeError, AttributeError):
            return "10m"

    def _request_dispatch(self) -> None:
        self._dispatch_event.set()

    async def _wait_for_dispatch_signal(self) -> None:
        if self._stop_event.is_set():
            return
        if self._dispatch_event.is_set():
            self._dispatch_event.clear()
            return
        try:
            await asyncio.wait_for(self._dispatch_event.wait(), timeout=self.poll_interval)
        except asyncio.TimeoutError:
            return
        finally:
            self._dispatch_event.clear()

    async def _get_inflight_snapshot(self) -> set[str]:
        async with self._inflight_lock:
            return set(self._inflight)

    async def _mark_inflight(self, task_id: str) -> None:
        async with self._inflight_lock:
            self._inflight.add(task_id)

    async def _unmark_inflight(self, task_id: str) -> None:
        async with self._inflight_lock:
            self._inflight.discard(task_id)


task_runner = BackgroundTaskRunner()
