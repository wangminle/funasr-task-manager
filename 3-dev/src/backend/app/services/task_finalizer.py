"""Task finalizer: merge segment results and transition parent task to terminal state."""

from __future__ import annotations

import asyncio
import shutil

from app.auth.rate_limiter import rate_limiter
from app.config import settings
from app.models import SegmentStatus, TaskStatus
from app.observability.logging import get_logger
from app.services.callback_worker import CallbackOutboxWorker
from app.services.result_formatter import to_json, to_srt, to_txt
from app.services.result_merger import SegmentInput, merge_segment_results
from app.storage.database import async_session_factory
from app.storage.file_manager import save_result
from app.storage.repository import SegmentRepository, TaskRepository

logger = get_logger(__name__)


class TaskFinalizer:
    """Coordinates segment merge and parent task finalization."""

    def __init__(self, callback_worker: CallbackOutboxWorker):
        self._callback_worker = callback_worker
        self._finalize_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task] = set()

    def _get_finalize_lock(self, task_id: str) -> asyncio.Lock:
        if task_id not in self._finalize_locks:
            self._finalize_locks[task_id] = asyncio.Lock()
        return self._finalize_locks[task_id]

    async def maybe_finalize(self, task_id: str) -> None:
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
            outbox = await self._callback_worker.enqueue(
                session, task, event.event_id, TaskStatus.SUCCEEDED,
            )
            if outbox is not None:
                pending_delivery = (outbox.outbox_id, task.callback_secret)
            user_id = task.user_id
            await session.commit()
            logger.info("segmented_task_succeeded", task_id=task_id,
                        segments=len(segments), merge_status=merge_status)
            await rate_limiter.record_task_completed(user_id)

        if pending_delivery:
            await self._callback_worker.try_deliver(*pending_delivery)

        task = asyncio.create_task(
            self._cleanup_segment_files(task_id),
            name=f"seg-cleanup-{task_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    async def _cleanup_segment_files(task_id: str) -> None:
        """Best-effort async cleanup of segment WAV files after successful merge."""
        seg_dir = settings.temp_dir / "segments" / task_id
        try:
            exists = await asyncio.to_thread(seg_dir.exists)
            if exists:
                await asyncio.to_thread(shutil.rmtree, seg_dir)
                logger.info("segment_files_cleaned", task_id=task_id, dir=str(seg_dir))
        except Exception as e:
            logger.warning("segment_files_cleanup_error", task_id=task_id, error=str(e))
