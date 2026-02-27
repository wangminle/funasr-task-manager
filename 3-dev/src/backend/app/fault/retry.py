"""Retry strategy with exponential backoff and server rotation."""

import asyncio
import random

from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_JITTER_FACTOR = 0.25


def calculate_delay(
    retry_count: int,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    jitter_factor: float = DEFAULT_JITTER_FACTOR,
) -> float:
    """Calculate exponential backoff delay with jitter.

    delay = min(base_delay * 2^retry_count, max_delay) ± jitter_factor
    """
    raw = min(base_delay * (2 ** retry_count), max_delay)
    jitter_range = raw * jitter_factor
    jitter = random.uniform(-jitter_range, jitter_range)
    return max(0.1, raw + jitter)


def select_retry_server(
    available_servers: list[str],
    failed_server: str | None = None,
) -> str | None:
    """Select a different server for retry, avoiding the one that just failed."""
    if not available_servers:
        return None
    candidates = [s for s in available_servers if s != failed_server]
    if not candidates:
        candidates = available_servers
    return random.choice(candidates)


class RetryPolicy:
    """Configurable retry policy for task execution."""

    def __init__(
        self,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        jitter_factor: float = DEFAULT_JITTER_FACTOR,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter_factor = jitter_factor

    def should_retry(self, retry_count: int) -> bool:
        return retry_count < self.max_retries

    def get_delay(self, retry_count: int) -> float:
        return calculate_delay(retry_count, self.base_delay, self.max_delay, self.jitter_factor)

    async def wait(self, retry_count: int) -> None:
        delay = self.get_delay(retry_count)
        logger.info("retry_waiting", retry_count=retry_count, delay=f"{delay:.1f}s")
        await asyncio.sleep(delay)

    def select_server(self, available: list[str], failed: str | None = None) -> str | None:
        return select_retry_server(available, failed)


default_retry_policy = RetryPolicy()
