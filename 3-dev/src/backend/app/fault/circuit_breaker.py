"""Circuit breaker pattern for ASR server fault tolerance."""

import time
from enum import StrEnum
from threading import Lock

from app.observability.logging import get_logger

logger = get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    def __init__(self, server_id: str, remaining_seconds: float):
        self.server_id = server_id
        self.remaining_seconds = remaining_seconds
        super().__init__(f"Circuit breaker OPEN for {server_id}, retry after {remaining_seconds:.0f}s")


class CircuitBreaker:
    """Per-server circuit breaker."""

    def __init__(
        self,
        server_id: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 3,
    ):
        self.server_id = server_id
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._last_failure_time: float = 0.0
        self._lock = Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    self._success_count = 0
                    logger.info("circuit_breaker_half_open", server_id=self.server_id)
            return self._state

    @property
    def state_value(self) -> int:
        """Numeric state for Prometheus: 0=CLOSED, 1=OPEN, 2=HALF_OPEN."""
        s = self.state
        return {"CLOSED": 0, "OPEN": 1, "HALF_OPEN": 2}.get(s, 0)

    def allow_request(self) -> bool:
        current = self.state
        if current == CircuitState.CLOSED:
            return True
        if current == CircuitState.HALF_OPEN:
            with self._lock:
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False
        return False

    def pre_check(self) -> None:
        if not self.allow_request():
            remaining = 0.0
            with self._lock:
                if self._state == CircuitState.OPEN:
                    elapsed = time.monotonic() - self._last_failure_time
                    remaining = max(self.recovery_timeout - elapsed, 0)
            raise CircuitBreakerOpenError(self.server_id, remaining)

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max_calls:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    logger.info("circuit_breaker_closed", server_id=self.server_id)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._half_open_calls = 0
                self._success_count = 0
                logger.warning("circuit_breaker_reopened", server_id=self.server_id)
            elif self._state == CircuitState.CLOSED and self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning("circuit_breaker_opened", server_id=self.server_id, failures=self._failure_count)

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0


class CircuitBreakerRegistry:
    """Manages circuit breakers for all servers."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0, half_open_max_calls: int = 3):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

    def get(self, server_id: str) -> CircuitBreaker:
        if server_id not in self._breakers:
            self._breakers[server_id] = CircuitBreaker(
                server_id=server_id,
                failure_threshold=self._failure_threshold,
                recovery_timeout=self._recovery_timeout,
                half_open_max_calls=self._half_open_max_calls,
            )
        return self._breakers[server_id]

    def get_all_states(self) -> dict[str, str]:
        return {sid: cb.state for sid, cb in self._breakers.items()}

    def remove(self, server_id: str) -> None:
        self._breakers.pop(server_id, None)


breaker_registry = CircuitBreakerRegistry()
