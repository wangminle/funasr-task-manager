"""Circuit breaker unit tests (T-M3-01 to T-M3-04)."""

import time

import pytest

from app.fault.circuit_breaker import (
    CircuitBreaker, CircuitBreakerOpenError, CircuitBreakerRegistry, CircuitState,
)


@pytest.mark.unit
class TestCircuitBreakerStates:
    def test_initial_state_closed(self):
        cb = CircuitBreaker("s1")
        assert cb.state == CircuitState.CLOSED

    def test_closed_to_open_after_failures(self):
        """T-M3-01: 5 consecutive failures → OPEN."""
        cb = CircuitBreaker("s1", failure_threshold=5)
        for _ in range(5):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_rejects_requests(self):
        """T-M3-02: OPEN state raises CircuitBreakerOpenError."""
        cb = CircuitBreaker("s1", failure_threshold=2, recovery_timeout=60.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            cb.pre_check()
        assert exc_info.value.server_id == "s1"

    def test_open_to_half_open_after_timeout(self):
        """T-M3-03: After recovery_timeout → HALF_OPEN."""
        cb = CircuitBreaker("s1", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_to_closed_after_successes(self):
        """T-M3-04: 3 successes in HALF_OPEN → CLOSED."""
        cb = CircuitBreaker("s1", failure_threshold=2, recovery_timeout=0.01, half_open_max_calls=3)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        for _ in range(3):
            assert cb.allow_request() is True
            cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker("s1", failure_threshold=2, recovery_timeout=0.01)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.02)
        assert cb.state == CircuitState.HALF_OPEN
        cb.allow_request()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("s1", failure_threshold=5)
        for _ in range(3):
            cb.record_failure()
        cb.record_success()
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_state_value_for_prometheus(self):
        cb = CircuitBreaker("s1", failure_threshold=2, recovery_timeout=0.01)
        assert cb.state_value == 0
        cb.record_failure()
        cb.record_failure()
        assert cb.state_value == 1
        time.sleep(0.02)
        assert cb.state_value == 2


@pytest.mark.unit
class TestCircuitBreakerRegistry:
    def test_get_creates_breaker(self):
        reg = CircuitBreakerRegistry()
        cb = reg.get("s1")
        assert cb.server_id == "s1"
        assert reg.get("s1") is cb

    def test_get_all_states(self):
        reg = CircuitBreakerRegistry()
        reg.get("s1")
        reg.get("s2")
        states = reg.get_all_states()
        assert states == {"s1": CircuitState.CLOSED, "s2": CircuitState.CLOSED}
