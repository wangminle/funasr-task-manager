"""Dramatiq-based distributed task runner for multi-instance ASR deployments.

STATUS: SCAFFOLD ONLY — NOT YET FUNCTIONAL
    The core transcription logic has not been extracted from BackgroundTaskRunner.
    Calling execute_transcription will raise NotImplementedError.
    This module is provided as the migration target for future multi-instance deployment.

Migration steps (when ready):
    1. Extract BackgroundTaskRunner._execute_task() into a shared async function.
    2. Call that shared function from _run_task() below.
    3. Install distributed dependencies: pip install "asr-task-manager[distributed]"
    4. Set ASR_REDIS_URL=redis://redis:6379/0
    5. Start Dramatiq worker: dramatiq app.services.task_runner_dramatiq
    6. In main.py, replace task_runner import with task_runner_distributed.

Architecture:
    When a task is created via the API, instead of being polled by BackgroundTaskRunner,
    it is dispatched to a Dramatiq queue backed by Redis. Multiple worker processes
    across multiple machines can then consume and execute jobs in parallel.
"""

from __future__ import annotations

try:
    import dramatiq
    from dramatiq.brokers.redis import RedisBroker
    HAS_DRAMATIQ = True
except ImportError:
    HAS_DRAMATIQ = False

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)


def _setup_broker() -> None:
    if not HAS_DRAMATIQ:
        raise RuntimeError(
            "Dramatiq is not installed. Run: pip install 'asr-task-manager[distributed]'"
        )
    broker = RedisBroker(url=settings.redis_url)
    dramatiq.set_broker(broker)
    logger.info("dramatiq_broker_configured", redis_url=settings.redis_url)


if HAS_DRAMATIQ:
    _setup_broker()

    @dramatiq.actor(max_retries=settings.max_retry_count, min_backoff=5000, max_backoff=60000)
    def execute_transcription(task_id: str) -> None:
        """Execute a single transcription job via Dramatiq worker.

        This is the distributed equivalent of BackgroundTaskRunner._process_task().
        The actual transcription logic should be extracted from task_runner.py into
        a shared function and called here.
        """
        import asyncio
        asyncio.run(_run_task(task_id))

    async def _run_task(task_id: str) -> None:
        from app.storage.database import async_session_factory
        from app.storage.repository import TaskRepository
        from app.models import TaskStatus

        logger.info("dramatiq_task_start", task_id=task_id)
        async with async_session_factory() as session:
            repo = TaskRepository(session)
            task = await repo.get_task(task_id)
            if not task or task.status not in (TaskStatus.QUEUED, TaskStatus.PENDING):
                logger.warning("dramatiq_task_skip", task_id=task_id, reason="invalid_state")
                return

        raise NotImplementedError(
            "Distributed transcription is not yet functional. "
            "The core transcription logic must be extracted from "
            "BackgroundTaskRunner._execute_task() into a shared async function "
            "before this module can be used. See task_runner.py for the reference implementation."
        )


class DistributedTaskRunner:
    """Facade that enqueues tasks to Dramatiq instead of running them in-process."""

    async def start(self) -> None:
        if not HAS_DRAMATIQ:
            logger.warning("dramatiq_not_available", msg="Falling back to in-process runner")
            return
        logger.warning(
            "distributed_task_runner_scaffold",
            msg="DistributedTaskRunner is a scaffold — transcription logic is NOT implemented yet",
        )

    async def stop(self) -> None:
        logger.info("distributed_task_runner_stopped")

    def enqueue(self, task_id: str) -> None:
        if not HAS_DRAMATIQ:
            raise RuntimeError("Dramatiq not installed")
        execute_transcription.send(task_id)
        logger.info("task_enqueued_to_dramatiq", task_id=task_id)


task_runner_distributed = DistributedTaskRunner()
