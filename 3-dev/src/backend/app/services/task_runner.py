"""Background task runner for queued ASR jobs."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.adapters.base import MessageProfile
from app.adapters.registry import get_adapter
from app.config import settings
from app.fault.circuit_breaker import breaker_registry
from app.models import File, ServerInstance, Task, TaskStatus, CallbackOutbox, OutboxStatus
from app.observability.logging import get_logger
from app.services.audio_preprocessor import ensure_wav, needs_conversion
from app.services.callback import create_outbox_record, deliver_callback, get_retry_delay, MAX_CALLBACK_RETRIES
from app.services.result_formatter import to_json, to_srt, to_txt
from app.services.scheduler import ScheduleDecision, ServerProfile, SlotQueue, scheduler as global_scheduler
from app.storage.database import async_session_factory
from app.storage.file_manager import save_result
from app.storage.repository import TaskRepository
from app.auth.rate_limiter import rate_limiter

logger = get_logger(__name__)


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

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._stop_event.clear()
        self._dispatch_event.set()
        self._loop_task = asyncio.create_task(self._run_loop(), name="asr-background-task-runner")
        logger.info("task_runner_started")

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
        """
        max_retries = settings.max_retry_count
        async with async_session_factory() as session:
            repo = TaskRepository(session)
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
        self._request_dispatch()

    async def _promote_preprocessing_tasks(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.preprocessing_delay_seconds)
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            stmt = (
                select(Task)
                .where(Task.status == TaskStatus.PREPROCESSING, Task.created_at <= cutoff)
                .order_by(Task.created_at.asc())
                .limit(100)
            )
            tasks = list((await session.execute(stmt)).scalars().all())
            if not tasks:
                return
            for task in tasks:
                if task.can_transition_to(TaskStatus.QUEUED):
                    await repo.update_task_status(task, TaskStatus.QUEUED)
            await session.commit()
        self._request_dispatch()

    async def _dispatch_queued_tasks(self) -> None:
        async with async_session_factory() as session:
            repo = TaskRepository(session)

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
                .where(Task.status == TaskStatus.QUEUED)
                .order_by(Task.created_at.asc())
                .limit(200)
            )
            queued_tasks = list((await session.execute(queued_stmt)).scalars().all())
            if not queued_tasks:
                return

            running_count: dict[str, int] = {}
            for srv in servers:
                count_stmt = select(Task).where(
                    Task.assigned_server_id == srv.server_id,
                    Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
                )
                running_count[srv.server_id] = len(list((await session.execute(count_stmt)).scalars().all()))

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
                if cb.allow_request():
                    available_profiles.append(sp)
            if not available_profiles:
                return

            dispatchable: list[tuple[Task, dict]] = []
            for task in queued_tasks:
                if task.task_id in inflight:
                    continue
                if not task.can_transition_to(TaskStatus.DISPATCHED):
                    continue
                audio_duration = 0.0
                if task.file and task.file.duration_sec:
                    audio_duration = task.file.duration_sec
                dispatchable.append((task, {
                    "task_id": task.task_id,
                    "audio_duration_sec": audio_duration,
                }))

            if not dispatchable:
                return

            # --- Slot-queue-aware planning and dispatch ---
            current_available_ids = frozenset(sp.server_id for sp in available_profiles)
            has_unplanned = not self._slot_queues or any(
                t.task_id not in self._planned_task_ids for t, _ in dispatchable
            )
            servers_changed = current_available_ids != self._planned_available_server_ids

            if has_unplanned or servers_changed:
                self._clear_slot_queues()
                batch_input = [d[1] for d in dispatchable]
                decisions = global_scheduler.schedule_batch(batch_input, available_profiles)
                if decisions:
                    self._slot_queues = global_scheduler.build_slot_queues(decisions)
                    self._planned_task_ids = {d.task_id for d in decisions}
                    self._planned_available_server_ids = current_available_ids

            if not self._slot_queues:
                return

            task_map = {t.task_id: t for t, _ in dispatchable}
            free_slots = {sp.server_id: max(sp.max_concurrency - sp.running_tasks, 0)
                          for sp in available_profiles}
            profile_map = {sp.server_id: sp for sp in available_profiles}

            to_start: list[str] = []

            # Phase A: dispatch from pre-planned slot queues
            for sq in list(self._slot_queues.values()):
                if free_slots.get(sq.server_id, 0) <= 0:
                    continue
                while sq.decisions and free_slots.get(sq.server_id, 0) > 0:
                    decision = sq.decisions[0]
                    task = task_map.get(decision.task_id)
                    if task is None or task.task_id in inflight or not task.can_transition_to(TaskStatus.DISPATCHED):
                        sq.decisions.pop(0)
                        self._planned_task_ids.discard(decision.task_id)
                        continue
                    task.assigned_server_id = decision.server_id
                    task.eta_seconds = int(decision.estimated_duration)
                    await repo.update_task_status(task, TaskStatus.DISPATCHED)
                    to_start.append(task.task_id)
                    sq.decisions.pop(0)
                    self._planned_task_ids.discard(decision.task_id)
                    free_slots[decision.server_id] -= 1
                    break

            # Phase B: work stealing — idle slots steal from other queues' tails
            for sp in available_profiles:
                sid = sp.server_id
                if free_slots.get(sid, 0) <= 0:
                    continue
                own_active_queues = sum(
                    1 for sq in self._slot_queues.values()
                    if sq.server_id == sid and sq.decisions
                )
                stealable_slots = free_slots.get(sid, 0) - own_active_queues
                if stealable_slots <= 0:
                    continue
                stolen_count = 0
                while stolen_count < stealable_slots:
                    result = self._find_steal_candidate(sp, profile_map, task_map, inflight)
                    if result is None:
                        break
                    decision, source_sq, est_stolen = result
                    task = task_map.get(decision.task_id)
                    if task is None or task.task_id in inflight or not task.can_transition_to(TaskStatus.DISPATCHED):
                        source_sq.decisions.remove(decision)
                        self._planned_task_ids.discard(decision.task_id)
                        continue
                    task.assigned_server_id = sid
                    task.eta_seconds = int(est_stolen)
                    await repo.update_task_status(task, TaskStatus.DISPATCHED)
                    to_start.append(task.task_id)
                    source_sq.decisions.remove(decision)
                    self._planned_task_ids.discard(decision.task_id)
                    free_slots[sid] -= 1
                    stolen_count += 1
                    logger.info("work_steal",
                                task_id=decision.task_id,
                                from_server=source_sq.server_id,
                                to_server=sid,
                                est_original=f"{decision.estimated_duration:.1f}s",
                                est_stolen=f"{est_stolen:.1f}s")

            # Cleanup exhausted queues
            self._slot_queues = {k: sq for k, sq in self._slot_queues.items() if sq.decisions}

            if not to_start:
                return
            await session.commit()

        for task_id in to_start:
            await self._mark_inflight(task_id)
            asyncio.create_task(self._execute_task(task_id), name=f"asr-task-{task_id}")

    async def _execute_task(self, task_id: str) -> None:
        server = None
        try:
            dispatch_info = await self._load_dispatch_info(task_id)
            if dispatch_info is None:
                return
            task, server, file_record = dispatch_info

            if not task.can_transition_to(TaskStatus.TRANSCRIBING):
                return

            async with async_session_factory() as session:
                repo = TaskRepository(session)
                db_task = await repo.get_task(task_id)
                if db_task is None:
                    return
                if db_task.can_transition_to(TaskStatus.TRANSCRIBING):
                    db_task.started_at = datetime.now(timezone.utc)
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
                breaker_registry.get(server.server_id).record_failure()
                await self._mark_task_failed(task_id, result.error)
                return

            if not result.text or not result.text.strip():
                breaker_registry.get(server.server_id).record_failure()
                await self._mark_task_failed(
                    task_id,
                    "ASR returned empty text (no transcription content received)",
                )
                return

            breaker_registry.get(server.server_id).record_success()

            audio_duration = 0.0
            if file_record and hasattr(file_record, "duration_sec") and file_record.duration_sec:
                audio_duration = file_record.duration_sec
            if audio_duration > 0 and task.started_at:
                actual_sec = (datetime.now(timezone.utc) - task.started_at).total_seconds()
                global_scheduler.calibrate_after_completion(
                    server_id=server.server_id,
                    audio_duration_sec=audio_duration,
                    actual_duration_sec=actual_sec,
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
                breaker_registry.get(server.server_id).record_failure()
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
            rate_limiter.record_task_completed(user_id)
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
                rate_limiter.record_task_completed(user_id)
        self._clear_slot_queues()
        self._request_dispatch()
        if pending_delivery:
            await self._try_deliver_outbox(*pending_delivery)

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
        task_map: dict[str, "Task"],
        inflight: set[str],
    ) -> tuple[ScheduleDecision, SlotQueue, float] | None:
        """Find a task to steal for an idle server.

        Scans other servers' queue tails (shortest tasks first, due to LPT ordering).
        Returns (decision, source_queue, est_processing_time_on_idle) or None.
        """
        best: tuple[ScheduleDecision, SlotQueue, float] | None = None
        best_improvement = 0.0

        for sq in self._slot_queues.values():
            if sq.server_id == idle_profile.server_id or not sq.decisions:
                continue
            for decision in reversed(sq.decisions):
                if decision.task_id in inflight:
                    continue
                task = task_map.get(decision.task_id)
                if task is None:
                    continue
                est_stolen = global_scheduler.estimate_processing_time(
                    decision.audio_duration_sec, idle_profile)
                decision_idx = sq.decisions.index(decision)
                source_remaining = (
                    sum(d.estimated_duration for d in sq.decisions[:decision_idx])
                    + decision.estimated_duration
                )
                improvement = source_remaining - est_stolen
                if improvement > 0 and improvement > best_improvement:
                    best = (decision, sq, est_stolen)
                    best_improvement = improvement
                break
        return best

    def _clear_slot_queues(self) -> None:
        self._slot_queues.clear()
        self._planned_task_ids.clear()
        self._planned_available_server_ids = frozenset()

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
