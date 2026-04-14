"""Rate limiting middleware - concurrent tasks, upload bandwidth, daily task count."""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field

from fastapi import HTTPException

from app.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class RateLimitConfig:
    max_concurrent_tasks: int = 10
    max_upload_bytes_per_minute: int = 50 * 1024 * 1024  # 50MB
    max_tasks_per_day: int = 100


@dataclass
class UserRateState:
    concurrent_tasks: int = 0
    upload_window: list[tuple[float, int]] = field(default_factory=list)
    daily_task_count: int = 0
    daily_reset_time: float = 0.0


class RateLimiter:
    """In-memory rate limiter (MVP). Replace with Redis in production."""

    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self._states: dict[str, UserRateState] = defaultdict(UserRateState)
        self._enabled = False
        self._lock = asyncio.Lock()

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_state(self, user_id: str) -> UserRateState:
        state = self._states[user_id]
        now = time.time()
        day_start = now - (now % 86400)
        if state.daily_reset_time < day_start:
            state.daily_task_count = 0
            state.daily_reset_time = day_start
        return state

    async def check_task_limits(self, user_id: str, count: int = 1) -> None:
        """Check both daily and concurrent limits in a single lock acquisition."""
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            if state.daily_task_count + count > self.config.max_tasks_per_day:
                raise HTTPException(
                    status_code=429,
                    detail=f"Daily task limit would be exceeded: "
                           f"{state.daily_task_count} used + {count} new > "
                           f"{self.config.max_tasks_per_day} max",
                )
            if state.concurrent_tasks + count > self.config.max_concurrent_tasks:
                raise HTTPException(
                    status_code=429,
                    detail=f"Concurrent task limit would be exceeded: "
                           f"{state.concurrent_tasks} existing + {count} new > "
                           f"{self.config.max_concurrent_tasks} max",
                )

    async def check_concurrent_tasks(self, user_id: str, count: int = 1) -> None:
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            if state.concurrent_tasks + count > self.config.max_concurrent_tasks:
                raise HTTPException(
                    status_code=429,
                    detail=f"Concurrent task limit would be exceeded: "
                           f"{state.concurrent_tasks} existing + {count} new > "
                           f"{self.config.max_concurrent_tasks} max",
                )

    async def check_daily_limit(self, user_id: str, count: int = 1) -> None:
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            if state.daily_task_count + count > self.config.max_tasks_per_day:
                raise HTTPException(
                    status_code=429,
                    detail=f"Daily task limit would be exceeded: "
                           f"{state.daily_task_count} used + {count} new > "
                           f"{self.config.max_tasks_per_day} max",
                )

    async def check_upload_bandwidth(self, user_id: str, file_size: int) -> None:
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            now = time.time()
            cutoff = now - 60.0
            state.upload_window = [(t, s) for t, s in state.upload_window if t > cutoff]
            total = sum(s for _, s in state.upload_window)
            if total + file_size > self.config.max_upload_bytes_per_minute:
                raise HTTPException(
                    status_code=429,
                    detail=f"Upload bandwidth limit exceeded ({self.config.max_upload_bytes_per_minute // 1024 // 1024}MB/min)",
                )

    async def record_upload(self, user_id: str, file_size: int) -> None:
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            state.upload_window.append((time.time(), file_size))

    async def record_task_created(self, user_id: str) -> None:
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            state.concurrent_tasks += 1
            state.daily_task_count += 1

    async def record_task_completed(self, user_id: str) -> None:
        if not self._enabled:
            return
        async with self._lock:
            state = self._get_state(user_id)
            state.concurrent_tasks = max(0, state.concurrent_tasks - 1)

    def get_user_stats(self, user_id: str) -> dict:
        state = self._get_state(user_id)
        return {
            "concurrent_tasks": state.concurrent_tasks,
            "daily_task_count": state.daily_task_count,
            "max_concurrent": self.config.max_concurrent_tasks,
            "max_daily": self.config.max_tasks_per_day,
        }


rate_limiter = RateLimiter()
