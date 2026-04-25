"""Background task runner for queued ASR jobs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.adapters.base import MessageProfile
from app.adapters.registry import get_adapter
from app.config import SEGMENT_LEVEL_PRESETS, settings
from app.fault.circuit_breaker import breaker_registry
from app.models import (
    File, ServerInstance, Task, TaskStatus,
    CallbackOutbox, OutboxStatus,
    TaskSegment, SegmentStatus,
)
from app.observability.logging import get_logger
from app.services.audio_preprocessor import (
    ensure_canonical_wav, ensure_wav, get_audio_duration_ms,
    needs_conversion, plan_segments, silence_detect, split_wav_segments,
)
from app.services.callback import create_outbox_record, deliver_callback, get_retry_delay, MAX_CALLBACK_RETRIES
from app.services.result_formatter import to_json, to_srt, to_txt
from app.services.result_merger import SegmentInput, merge_segment_results
from app.services.scheduler import ScheduleDecision, ServerProfile, SlotQueue, scheduler as global_scheduler
from app.storage.database import async_session_factory
from app.storage.file_manager import save_result
from app.storage.repository import SegmentRepository, TaskRepository
from app.auth.rate_limiter import rate_limiter

logger = get_logger(__name__)


REPLAN_IMBALANCE_RATIO = 1.5


class BackgroundTaskRunner:
    """Polls tasks and executes transcription jobs in-process."""

    def __init__(self, poll_interval: float = 1.0, preprocessing_delay_seconds: int = 2):
        self.poll_interval = poll_interval
        self.preprocessing_delay_seconds = preprocessing_delay_seconds
        self._loop_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._dispatch_event = asyncio.Event()
        self._inflight: set[str] = set()
        self._inflight_lock = asyncio.Lock()
        self._slot_queues: dict[str, SlotQueue] = {}
        self._planned_task_ids: set[str] = set()
        self._planned_available_server_ids: frozenset[str] = frozenset()
        self._finalize_locks: dict[str, asyncio.Lock] = {}

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
                await self._maybe_finalize_segmented_task(tid)
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
                    await self._retry_pending_callbacks()
                    callback_tick = 0
            except Exception as e:
                logger.exception("task_runner_loop_error", error=str(e))
            await self._wait_for_dispatch_signal()

    async def _retry_failed_tasks(self) -> None:
        """Re-queue FAILED tasks for automatic retry (up to max_retry_count).

        Goes directly FAILED → QUEUED so the dispatcher picks them up
        immediately. The file is already uploaded and preprocessed, so
        there is no need to revisit PENDING/PREPROCESSING.

        Segmented tasks are skipped — their retry happens at the segment
        level inside ``_mark_segment_failed``.  The exception is
        MERGE_FAILED tasks where all segments succeeded but the final
        merge step hit a transient error; those are retried by pushing
        the parent back to TRANSCRIBING and re-triggering finalize.
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
            await self._maybe_finalize_segmented_task(tid)
        self._request_dispatch()

    async def _promote_preprocessing_tasks(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.preprocessing_delay_seconds)

        candidates: list[tuple[str, float, str, str | None]] = []
        async with async_session_factory() as session:
            stmt = (
                select(Task)
                .options(selectinload(Task.file))
                .where(Task.status == TaskStatus.PREPROCESSING, Task.created_at <= cutoff)
                .order_by(Task.created_at.asc())
                .limit(100)
            )
            tasks = list((await session.execute(stmt)).scalars().all())
            if not tasks:
                return
            for task in tasks:
                if not task.can_transition_to(TaskStatus.QUEUED):
                    continue
                duration_sec = task.file.duration_sec if task.file and task.file.duration_sec else 0
                audio_path = task.file.storage_path if task.file else ""
                candidates.append((task.task_id, duration_sec or 0, audio_path, task.options_json))

        if not candidates:
            return

        for task_id, duration_sec, audio_path, options_json in candidates:
            auto_seg = self._parse_auto_segment(options_json)
            seg_level = self._parse_segment_level(options_json)

            if auto_seg == "off":
                needs_segmentation = False
            elif auto_seg == "on":
                needs_segmentation = bool(audio_path) and settings.segment_enabled
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
                    if auto_seg == "on":
                        logger.error(
                            "segmentation_failed_explicit_on",
                            task_id=task_id,
                            error=str(e),
                        )
                        async with async_session_factory() as session:
                            repo = TaskRepository(session)
                            task = await repo.get_task(task_id)
                            if task and task.can_transition_to(TaskStatus.FAILED):
                                task.error_code = "SEGMENTATION_FAILED"
                                task.error_message = f"User requested segmentation but it failed: {e}"
                                await repo.update_task_status(task, TaskStatus.FAILED)
                                await session.commit()
                        continue
                    logger.warning(
                        "segmentation_failed_fallback_to_whole_file",
                        task_id=task_id,
                        error=str(e),
                        hint="Will proceed as unsegmented task",
                    )

            async with async_session_factory() as session:
                repo = TaskRepository(session)
                task = await repo.get_task(task_id)
                if task and task.can_transition_to(TaskStatus.QUEUED):
                    await repo.update_task_status(task, TaskStatus.QUEUED)
                    await session.commit()

        self._request_dispatch()

    async def _create_segments_for_task(
        self, task_id: str, audio_path: str, *, seg_level: str = "10m",
    ) -> None:
        """Execute canonical WAV → silence detect → plan → split → write segments.

        Heavy I/O (ffmpeg) happens outside any database session.  Writing
        segment records uses a short dedicated session with an idempotency
        guard to handle runner restarts safely.

        On failure, any intermediate files (canonical WAV, partial segments)
        are cleaned up to avoid disk leaks.
        """
        import shutil
        from ulid import ULID

        async with async_session_factory() as session:
            seg_repo = SegmentRepository(session)
            existing = await seg_repo.list_segments_by_task(task_id)
            if existing:
                logger.info("segments_already_exist", task_id=task_id, count=len(existing))
                return

        output_dir_path = settings.temp_dir / "segments" / task_id
        canonical_path: str | None = None
        try:
            canonical_path = await ensure_canonical_wav(audio_path)
            duration_ms = await get_audio_duration_ms(canonical_path)
            silence_ranges = await silence_detect(canonical_path)

            plan_kwargs: dict[str, int] = {}
            preset = SEGMENT_LEVEL_PRESETS.get(seg_level)
            if preset and seg_level != "10m":
                plan_kwargs["target_duration_ms"] = preset.target_duration_sec * 1000
                plan_kwargs["max_duration_ms"] = preset.max_duration_sec * 1000
            plans = plan_segments(duration_ms, silence_ranges, **plan_kwargs)

            if len(plans) <= 1:
                logger.info("segmentation_single_segment", task_id=task_id,
                            duration_ms=duration_ms, hint="below split threshold after planning")
                return

            segment_paths = await split_wav_segments(
                canonical_path, plans, str(output_dir_path), task_id,
            )

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
                    for plan, path in zip(plans, segment_paths)
                ]
                await seg_repo.create_segments(segments)
                await session.commit()

            logger.info("segments_created", task_id=task_id, count=len(plans),
                         duration_ms=duration_ms, silence_ranges=len(silence_ranges))
        except Exception:
            if output_dir_path.exists():
                try:
                    shutil.rmtree(output_dir_path)
                except OSError:
                    pass
            raise

    async def _dispatch_queued_tasks(self) -> None:
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            seg_repo = SegmentRepository(session)

            servers_stmt = (
                select(ServerInstance)
                .where(ServerInstance.status == "ONLINE")
                .order_by(ServerInstance.server_id.asc())
            )
            servers = list((await session.execute(servers_stmt)).scalars().all())
            if not servers:
                self._clear_slot_queues()
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

            count_stmt = (
                select(Task.assigned_server_id, func.count())
                .where(Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]))
                .group_by(Task.assigned_server_id)
            )
            running_count: dict[str, int] = dict(
                (await session.execute(count_stmt)).all()
            )

            # Also count in-flight segments assigned to each server
            seg_server_count_stmt = (
                select(TaskSegment.assigned_server_id, func.count())
                .where(TaskSegment.status.in_([
                    SegmentStatus.DISPATCHED, SegmentStatus.TRANSCRIBING,
                ]))
                .group_by(TaskSegment.assigned_server_id)
            )
            seg_running: dict[str, int] = dict(
                (await session.execute(seg_server_count_stmt)).all()
            )
            for sid, cnt in seg_running.items():
                if sid:
                    running_count[sid] = running_count.get(sid, 0) + cnt

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
                if await cb.allow_request():
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

            # --- Slot-queue-aware planning and dispatch ---
            current_available_ids = frozenset(sp.server_id for sp in available_profiles)
            has_unplanned = not self._slot_queues or any(
                wi["task_id"] not in self._planned_task_ids for wi in work_items
            )
            servers_changed = current_available_ids != self._planned_available_server_ids
            queue_imbalanced = self._check_queue_imbalance(current_available_ids)

            if has_unplanned or servers_changed or queue_imbalanced:
                self._clear_slot_queues()
                decisions = global_scheduler.schedule_batch(work_items, available_profiles)
                if decisions:
                    self._slot_queues = global_scheduler.build_slot_queues(decisions)
                    self._planned_task_ids = {d.task_id for d in decisions}
                    self._planned_available_server_ids = current_available_ids

            if not self._slot_queues:
                return

            work_map: dict[str, object] = {}
            work_map.update(regular_tasks)
            work_map.update({sid: seg for sid, (seg, _) in segment_items.items()})

            free_slots = {sp.server_id: max(sp.max_concurrency - sp.running_tasks, 0)
                          for sp in available_profiles}
            profile_map = {sp.server_id: sp for sp in available_profiles}

            to_start_tasks: list[str] = []
            to_start_segments: list[str] = []

            # Phase A: dispatch from pre-planned slot queues
            for sq in list(self._slot_queues.values()):
                if free_slots.get(sq.server_id, 0) <= 0:
                    continue
                while sq.decisions and free_slots.get(sq.server_id, 0) > 0:
                    decision = sq.decisions[0]

                    if decision.kind == "segment":
                        seg_tuple = segment_items.get(decision.task_id)
                        if seg_tuple is None or decision.task_id in inflight:
                            sq.decisions.pop(0)
                            self._planned_task_ids.discard(decision.task_id)
                            continue
                        seg, parent_task = seg_tuple
                        await seg_repo.update_segment_status(
                            seg, SegmentStatus.DISPATCHED, server_id=decision.server_id,
                        )
                        await session.refresh(parent_task, ["status"])
                        if parent_task.status == TaskStatus.QUEUED.value:
                            if parent_task.can_transition_to(TaskStatus.DISPATCHED):
                                await repo.update_task_status(parent_task, TaskStatus.DISPATCHED)
                        to_start_segments.append(seg.segment_id)
                    else:
                        task = regular_tasks.get(decision.task_id)
                        if (task is None or task.task_id in inflight
                                or not task.can_transition_to(TaskStatus.DISPATCHED)):
                            sq.decisions.pop(0)
                            self._planned_task_ids.discard(decision.task_id)
                            continue
                        task.assigned_server_id = decision.server_id
                        task.eta_seconds = int(decision.estimated_duration)
                        await repo.update_task_status(task, TaskStatus.DISPATCHED)
                        to_start_tasks.append(task.task_id)

                    sq.decisions.pop(0)
                    self._planned_task_ids.discard(decision.task_id)
                    free_slots[decision.server_id] -= 1
                    break

            # Phase B: work stealing — any server with free slots can steal
            for sp in available_profiles:
                sid = sp.server_id
                while free_slots.get(sid, 0) > 0:
                    result = self._find_steal_candidate(sp, profile_map, work_map, inflight)
                    if result is None:
                        break
                    decision, source_sq, est_stolen = result

                    if decision.kind == "segment":
                        seg_tuple = segment_items.get(decision.task_id)
                        if seg_tuple is None or decision.task_id in inflight:
                            source_sq.decisions.remove(decision)
                            self._planned_task_ids.discard(decision.task_id)
                            continue
                        seg, parent_task = seg_tuple
                        active_count = await seg_repo.count_active_segments(parent_task.task_id)
                        steal_max = min(len(available_profiles), settings.segment_max_parallel_per_task)
                        if active_count >= steal_max:
                            source_sq.decisions.remove(decision)
                            self._planned_task_ids.discard(decision.task_id)
                            continue
                        await seg_repo.update_segment_status(
                            seg, SegmentStatus.DISPATCHED, server_id=sid,
                        )
                        await session.refresh(parent_task, ["status"])
                        if parent_task.status == TaskStatus.QUEUED.value:
                            if parent_task.can_transition_to(TaskStatus.DISPATCHED):
                                await repo.update_task_status(parent_task, TaskStatus.DISPATCHED)
                        to_start_segments.append(seg.segment_id)
                    else:
                        task = regular_tasks.get(decision.task_id)
                        if (task is None or task.task_id in inflight
                                or not task.can_transition_to(TaskStatus.DISPATCHED)):
                            source_sq.decisions.remove(decision)
                            self._planned_task_ids.discard(decision.task_id)
                            continue
                        task.assigned_server_id = sid
                        task.eta_seconds = int(est_stolen)
                        await repo.update_task_status(task, TaskStatus.DISPATCHED)
                        to_start_tasks.append(task.task_id)

                    source_sq.decisions.remove(decision)
                    self._planned_task_ids.discard(decision.task_id)
                    free_slots[sid] -= 1
                    logger.info("work_steal",
                                work_id=decision.task_id,
                                kind=decision.kind,
                                from_server=source_sq.server_id,
                                to_server=sid,
                                est_original=f"{decision.estimated_duration:.1f}s",
                                est_stolen=f"{est_stolen:.1f}s")

            # Cleanup exhausted queues
            self._slot_queues = {k: sq for k, sq in self._slot_queues.items() if sq.decisions}

            if not to_start_tasks and not to_start_segments:
                return
            await session.commit()

        for task_id in to_start_tasks:
            await self._mark_inflight(task_id)
            asyncio.create_task(self._execute_task(task_id), name=f"asr-task-{task_id}")

        for segment_id in to_start_segments:
            await self._mark_inflight(segment_id)
            asyncio.create_task(self._execute_segment(segment_id), name=f"asr-seg-{segment_id}")

    async def _execute_task(self, task_id: str) -> None:
        server = None
        try:
            dispatch_info = await self._load_dispatch_info(task_id)
            if dispatch_info is None:
                return
            task, server, file_record = dispatch_info

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
            )

            if result.error:
                await breaker_registry.get(server.server_id).record_failure()
                await self._mark_task_failed(task_id, result.error)
                return

            if not result.text or not result.text.strip():
                logger.warning(
                    "asr_empty_text",
                    task_id=task_id,
                    server_id=server.server_id,
                    hint="Silent audio or no speech detected",
                )
                await breaker_registry.get(server.server_id).record_success()
                result.text = ""
            else:
                await breaker_registry.get(server.server_id).record_success()

            audio_duration = 0.0
            if file_record and hasattr(file_record, "duration_sec") and file_record.duration_sec:
                audio_duration = file_record.duration_sec
            if audio_duration > 0 and task_started_at:
                actual_sec = (datetime.now(timezone.utc) - task_started_at).total_seconds()
                global_scheduler.calibrate_after_completion(
                    server_id=server.server_id,
                    audio_duration_sec=audio_duration,
                    actual_duration_sec=actual_sec,
                    predicted_duration_sec=float(task.eta_seconds) if task.eta_seconds else None,
                )

            raw = result.raw if isinstance(result.raw, dict) and result.raw else {}
            if "text" not in raw:
                raw["text"] = result.text
            if "mode" not in raw:
                raw["mode"] = result.mode

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
                outbox = await self._enqueue_callback(session, task, event.event_id, TaskStatus.SUCCEEDED)
                if outbox is not None:
                    pending_delivery = (outbox.outbox_id, task.callback_secret)
            user_id = task.user_id
            await session.commit()
            logger.info("task_transcription_succeeded", task_id=task_id)
            await rate_limiter.record_task_completed(user_id)
        if not any(sq.decisions for sq in self._slot_queues.values()):
            self._clear_slot_queues()
        self._request_dispatch()
        if pending_delivery:
            await self._try_deliver_outbox(*pending_delivery)

    async def _mark_task_failed(self, task_id: str, message: str) -> None:
        pending_delivery = None
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task is None:
                return
            task.error_code = "TRANSCRIBE_ERROR"
            task.error_message = message[:2000]
            is_terminal = task.retry_count >= settings.max_retry_count
            if task.can_transition_to(TaskStatus.FAILED):
                event = await repo.update_task_status(task, TaskStatus.FAILED)
                if is_terminal:
                    outbox = await self._enqueue_callback(session, task, event.event_id, TaskStatus.FAILED, error_message=message[:2000])
                    if outbox is not None:
                        pending_delivery = (outbox.outbox_id, task.callback_secret)
            user_id = task.user_id
            await session.commit()
            logger.warning("task_transcription_failed", task_id=task_id,
                           error=message[:300], terminal=is_terminal,
                           retry_count=task.retry_count)
            if is_terminal:
                await rate_limiter.record_task_completed(user_id)
        if not any(sq.decisions for sq in self._slot_queues.values()):
            self._clear_slot_queues()
        self._request_dispatch()
        if pending_delivery:
            await self._try_deliver_outbox(*pending_delivery)

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

            if parent_task.status in (TaskStatus.CANCELED.value, TaskStatus.FAILED.value):
                logger.info("segment_skipped_parent_terminal",
                            segment_id=segment_id, parent_status=parent_task.status)
                async with async_session_factory() as session:
                    seg_repo = SegmentRepository(session)
                    seg = await seg_repo.get_segment(segment_id)
                    if seg and seg.status in (
                        SegmentStatus.DISPATCHED, SegmentStatus.PENDING,
                    ):
                        await seg_repo.update_segment_status(
                            seg, SegmentStatus.FAILED,
                            error_message=f"Parent task {parent_task.status}",
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
            )

            if result.error:
                await breaker_registry.get(server.server_id).record_failure()
                await self._mark_segment_failed(segment_id, result.error)
                return

            if not result.text or not result.text.strip():
                await breaker_registry.get(server.server_id).record_success()
                result.text = ""
            else:
                await breaker_registry.get(server.server_id).record_success()

            seg_audio_duration = segment.duration_ms / 1000.0
            if seg_audio_duration > 0 and segment_started_at:
                actual_sec = (datetime.now(timezone.utc) - segment_started_at).total_seconds()
                predicted_sec = global_scheduler.rtf_tracker.get_p90(server.server_id) * seg_audio_duration
                global_scheduler.calibrate_after_completion(
                    server_id=server.server_id,
                    audio_duration_sec=seg_audio_duration,
                    actual_duration_sec=actual_sec,
                    predicted_duration_sec=predicted_sec if predicted_sec > 0 else None,
                )

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

            await seg_repo.update_segment_status(
                segment, SegmentStatus.SUCCEEDED, raw_result_json=raw_result_json,
            )

            total_keep = await seg_repo.total_keep_duration_ms(task_id)
            completed_keep = await seg_repo.sum_completed_duration_ms(task_id)

            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task and total_keep > 0:
                progress = 0.20 + 0.75 * (completed_keep / total_keep)
                task.progress = min(progress, 0.95)

            await session.commit()

        logger.info("segment_transcription_succeeded",
                     segment_id=segment_id, task_id=task_id,
                     completed_keep_ms=completed_keep, total_keep_ms=total_keep)

        if task_id:
            await self._maybe_finalize_segmented_task(task_id)

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

            await seg_repo.update_segment_status(
                segment, SegmentStatus.FAILED, error_message=message,
            )

            if segment.retry_count < settings.segment_max_retry_count:
                await seg_repo.increment_retry(segment)
                await session.commit()
                logger.info("segment_retry_queued",
                            segment_id=segment_id,
                            task_id=segment.task_id,
                            retry=segment.retry_count)
            else:
                repo = TaskRepository(session)
                task = await repo.get_task(segment.task_id)
                if task and task.can_transition_to(TaskStatus.FAILED):
                    task.error_code = "SEGMENT_RETRY_EXHAUSTED"
                    error_msg = (
                        f"Segment {segment.segment_index} failed after "
                        f"{segment.retry_count} retries: {message[:500]}"
                    )
                    task.error_message = error_msg
                    event = await repo.update_task_status(task, TaskStatus.FAILED)
                    outbox = await self._enqueue_callback(
                        session, task, event.event_id, TaskStatus.FAILED,
                        error_message=error_msg,
                    )
                    if outbox is not None:
                        pending_delivery = (outbox.outbox_id, task.callback_secret)
                    user_id = task.user_id
                    parent_failed = True
                await session.commit()
                logger.warning("segment_retry_exhausted",
                               segment_id=segment_id,
                               task_id=segment.task_id,
                               segment_index=segment.segment_index,
                               retry_count=segment.retry_count)

        if parent_failed and user_id:
            await rate_limiter.record_task_completed(user_id)
        if not any(sq.decisions for sq in self._slot_queues.values()):
            self._clear_slot_queues()
        self._request_dispatch()
        if pending_delivery:
            await self._try_deliver_outbox(*pending_delivery)

    # ------------------------------------------------------------------
    # Stage 8: Parent-task merge & terminal state
    # ------------------------------------------------------------------

    def _get_finalize_lock(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._finalize_locks:
            self._finalize_locks[task_id] = asyncio.Lock()
        return self._finalize_locks[task_id]

    async def _maybe_finalize_segmented_task(self, task_id: str) -> None:
        """Check whether all segments are done and merge results if so.

        Uses a per-task asyncio.Lock plus a DB-level status double-check
        to guarantee the merge executes at most once even when multiple
        segments complete near-simultaneously.
        """
        lock = self._get_finalize_lock(task_id)
        async with lock:
            async with async_session_factory() as session:
                seg_repo = SegmentRepository(session)
                status_counts = await seg_repo.count_by_status(task_id)
                total = sum(status_counts.values())
                if total == 0:
                    return

                succeeded = status_counts.get(SegmentStatus.SUCCEEDED, 0)
                failed = status_counts.get(SegmentStatus.FAILED, 0)
                pending = status_counts.get(SegmentStatus.PENDING, 0)
                active = (status_counts.get(SegmentStatus.DISPATCHED, 0)
                          + status_counts.get(SegmentStatus.TRANSCRIBING, 0))

                if active > 0 or pending > 0:
                    return

                repo = TaskRepository(session)
                task = await repo.get_task(task_id)
                if task is None or task.status in (
                    TaskStatus.SUCCEEDED.value,
                    TaskStatus.CANCELED.value,
                    TaskStatus.FAILED.value,
                ):
                    return

                if failed > 0:
                    return

                if succeeded != total:
                    return

            try:
                await self._merge_and_finalize(task_id)
            except Exception as e:
                logger.exception("segment_merge_failed", task_id=task_id, error=str(e))
                async with async_session_factory() as session:
                    repo = TaskRepository(session)
                    task = await repo.get_task(task_id)
                    if task and task.can_transition_to(TaskStatus.FAILED):
                        task.error_code = "MERGE_FAILED"
                        task.error_message = f"Segment result merge failed: {str(e)[:500]}"
                        await repo.update_task_status(task, TaskStatus.FAILED)
                        await session.commit()
            finally:
                self._finalize_locks.pop(task_id, None)

    async def _merge_and_finalize(self, task_id: str) -> None:
        """Merge all segment results and mark the parent task SUCCEEDED."""
        async with async_session_factory() as session:
            seg_repo = SegmentRepository(session)
            segments = await seg_repo.list_segments_by_task(task_id)

            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task is None or task.status == TaskStatus.SUCCEEDED.value:
                return

        seg_inputs = [
            SegmentInput(
                segment_index=seg.segment_index,
                source_start_ms=seg.source_start_ms,
                keep_start_ms=seg.keep_start_ms,
                keep_end_ms=seg.keep_end_ms,
                raw_result_json=seg.raw_result_json or "{}",
            )
            for seg in segments
        ]

        merged_result, merge_status = merge_segment_results(seg_inputs)
        logger.info("segment_merge_complete", task_id=task_id,
                     segments=len(segments), merge_status=merge_status,
                     text_length=len(merged_result.get("text", "")))

        await save_result(task_id, to_json(merged_result), "json")
        await save_result(task_id, to_txt(merged_result), "txt")
        await save_result(task_id, to_srt(merged_result), "srt")

        pending_delivery = None
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if task is None:
                return
            if not task.can_transition_to(TaskStatus.SUCCEEDED):
                return

            task.result_path = task_id
            task.error_code = None
            task.error_message = None
            event = await repo.update_task_status(task, TaskStatus.SUCCEEDED)
            outbox = await self._enqueue_callback(
                session, task, event.event_id, TaskStatus.SUCCEEDED,
            )
            if outbox is not None:
                pending_delivery = (outbox.outbox_id, task.callback_secret)
            user_id = task.user_id
            await session.commit()
            logger.info("segmented_task_succeeded", task_id=task_id,
                        segments=len(segments), merge_status=merge_status)
            await rate_limiter.record_task_completed(user_id)

        if not any(sq.decisions for sq in self._slot_queues.values()):
            self._clear_slot_queues()
        if pending_delivery:
            await self._try_deliver_outbox(*pending_delivery)

        asyncio.create_task(
            self._cleanup_segment_files(task_id),
            name=f"seg-cleanup-{task_id}",
        )

    @staticmethod
    async def _cleanup_segment_files(task_id: str) -> None:
        """Best-effort async cleanup of segment WAV files after successful merge."""
        import shutil
        seg_dir = settings.temp_dir / "segments" / task_id
        try:
            exists = await asyncio.to_thread(seg_dir.exists)
            if exists:
                await asyncio.to_thread(shutil.rmtree, seg_dir)
                logger.info("segment_files_cleaned", task_id=task_id, dir=str(seg_dir))
        except Exception as e:
            logger.warning("segment_files_cleanup_error", task_id=task_id, error=str(e))

    # ------------------------------------------------------------------
    # Callback & misc helpers
    # ------------------------------------------------------------------

    async def _enqueue_callback(
        self,
        session,
        task: Task,
        event_id: str,
        status: TaskStatus,
        error_message: str | None = None,
    ) -> CallbackOutbox | None:
        """Write outbox record within the current transaction.

        Returns the outbox record so the caller can attempt delivery AFTER commit.
        """
        if not task.callback_url:
            return None
        outbox = create_outbox_record(
            task_id=task.task_id,
            event_id=event_id,
            callback_url=task.callback_url,
            status=status.value,
            progress=task.progress,
            result_path=task.result_path,
            error_message=error_message,
        )
        session.add(outbox)
        await session.flush()
        return outbox

    async def _try_deliver_outbox(self, outbox_id: str, callback_secret: str | None) -> None:
        """Best-effort immediate delivery using a dedicated session (post-commit)."""
        try:
            async with async_session_factory() as session:
                stmt = select(CallbackOutbox).where(CallbackOutbox.outbox_id == outbox_id)
                outbox = (await session.execute(stmt)).scalar_one_or_none()
                if outbox is None or outbox.status != OutboxStatus.PENDING.value:
                    return
                await deliver_callback(outbox, secret=callback_secret)
                await session.commit()
        except Exception as e:
            logger.warning("callback_immediate_delivery_failed", outbox_id=outbox_id, error=str(e))

    async def _retry_pending_callbacks(self) -> None:
        """Scan callback_outbox for undelivered PENDING records and retry."""
        async with async_session_factory() as session:
            stmt = (
                select(CallbackOutbox)
                .where(
                    CallbackOutbox.status == OutboxStatus.PENDING.value,
                    CallbackOutbox.retry_count < MAX_CALLBACK_RETRIES,
                )
                .order_by(CallbackOutbox.created_at.asc())
                .limit(50)
            )
            records = list((await session.execute(stmt)).scalars().all())
            if not records:
                return
            for outbox in records:
                task_stmt = select(Task.callback_secret).where(Task.task_id == outbox.task_id)
                secret = (await session.execute(task_stmt)).scalar_one_or_none()
                try:
                    await deliver_callback(outbox, secret=secret)
                except Exception as e:
                    logger.warning("callback_retry_error", outbox_id=outbox.outbox_id, error=str(e))
            await session.commit()
            delivered = sum(1 for r in records if r.status == OutboxStatus.SENT.value)
            if delivered:
                logger.info("callback_outbox_retry_batch", total=len(records), delivered=delivered)

    async def _transcribe_with_protocol_fallback(
        self,
        *,
        adapter,
        server: ServerInstance,
        audio_path: str,
        profile: MessageProfile,
    ):
        timeout = float(settings.task_timeout_seconds)

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
    ) -> tuple[ScheduleDecision, SlotQueue, float] | None:
        """Find the best work item to steal for an idle server.

        Scans other servers' queue tails, checking up to max_candidates_per_queue
        items per queue to find the candidate with the greatest improvement.
        Returns (decision, source_queue, est_processing_time_on_idle) or None.
        """
        best: tuple[ScheduleDecision, SlotQueue, float] | None = None
        best_improvement = 0.0

        for sq in self._slot_queues.values():
            if sq.server_id == idle_profile.server_id or not sq.decisions:
                continue
            checked = 0
            for idx_from_end, decision in enumerate(reversed(sq.decisions)):
                if checked >= max_candidates_per_queue:
                    break
                if decision.task_id in inflight:
                    continue
                if work_map.get(decision.task_id) is None:
                    continue
                checked += 1
                est_stolen = global_scheduler.estimate_processing_time(
                    decision.audio_duration_sec, idle_profile)
                decision_idx = len(sq.decisions) - 1 - idx_from_end
                source_remaining = (
                    sum(d.estimated_duration for d in sq.decisions[:decision_idx])
                    + decision.estimated_duration
                )
                improvement = source_remaining - est_stolen
                if improvement > 0 and improvement > best_improvement:
                    best = (decision, sq, est_stolen)
                    best_improvement = improvement
        return best

    def _check_queue_imbalance(self, available_server_ids: frozenset[str]) -> bool:
        """Detect if remaining slot queue workload is significantly imbalanced.

        Triggers re-plan when:
        1. A server that was part of the active plan has exhausted its queue
           (removed by cleanup) while other planned servers still have work.
        2. The ratio of max-to-min remaining work across planned servers
           exceeds REPLAN_IMBALANCE_RATIO.
        """
        if not self._slot_queues:
            return False

        server_remaining: dict[str, float] = {}
        for sq in self._slot_queues.values():
            total = sum(d.estimated_duration for d in sq.decisions)
            server_remaining[sq.server_id] = server_remaining.get(sq.server_id, 0.0) + total

        has_backlog = any(v > 0.0 for v in server_remaining.values())
        if not has_backlog:
            return False

        exhausted_ids = (
            self._planned_available_server_ids - set(server_remaining.keys())
        ) & available_server_ids
        if exhausted_ids:
            logger.info("queue_imbalance_idle_server",
                        exhausted=list(exhausted_ids),
                        remaining={sid: f"{v:.1f}s" for sid, v in server_remaining.items()})
            return True

        positives = [v for v in server_remaining.values() if v > 0]
        if len(positives) >= 2:
            ratio = max(positives) / min(positives)
            if ratio > REPLAN_IMBALANCE_RATIO:
                logger.info("queue_imbalance_ratio",
                            ratio=f"{ratio:.2f}",
                            threshold=f"{REPLAN_IMBALANCE_RATIO:.1f}",
                            remaining={sid: f"{v:.1f}s" for sid, v in server_remaining.items()})
                return True

        return False

    def _clear_slot_queues(self) -> None:
        self._slot_queues.clear()
        self._planned_task_ids.clear()
        self._planned_available_server_ids = frozenset()

    @staticmethod
    def _parse_auto_segment(options_json: str | None) -> str:
        """Extract auto_segment preference from task options_json.

        Returns ``"auto"`` (default), ``"on"``, or ``"off"``.
        """
        if not options_json:
            return "auto"
        try:
            opts = json.loads(options_json)
            value = opts.get("auto_segment", "auto")
            return value if value in ("auto", "on", "off") else "auto"
        except (json.JSONDecodeError, AttributeError):
            return "auto"

    @staticmethod
    def _parse_segment_level(options_json: str | None) -> str:
        """Extract segment_level preference from task options_json.

        Returns ``"10m"`` (default), ``"20m"``, or ``"30m"``.
        """
        if not options_json:
            return "10m"
        try:
            opts = json.loads(options_json)
            value = opts.get("segment_level", "10m")
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
