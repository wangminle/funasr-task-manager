"""Callback outbox worker: enqueue, deliver, and retry webhook callbacks."""

from __future__ import annotations

from sqlalchemy import select

from app.models import CallbackOutbox, OutboxStatus, Task, TaskStatus
from app.observability.logging import get_logger
from app.services.callback import (
    MAX_CALLBACK_RETRIES,
    create_outbox_record,
    deliver_callback,
)
from app.storage.database import async_session_factory

logger = get_logger(__name__)


class CallbackOutboxWorker:
    """Manages callback lifecycle: enqueue within transaction, deliver, retry."""

    async def enqueue(
        self,
        session,
        task: Task,
        event_id: str,
        status: TaskStatus,
        error_message: str | None = None,
    ) -> CallbackOutbox | None:
        """Write outbox record within the caller's transaction.

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

    async def try_deliver(self, outbox_id: str, callback_secret: str | None) -> None:
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

    async def retry_pending(self) -> None:
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


callback_worker = CallbackOutboxWorker()
