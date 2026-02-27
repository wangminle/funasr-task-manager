"""Retry strategy unit tests (T-M3-05 to T-M3-07)."""

import pytest

from app.fault.retry import (
    RetryPolicy, calculate_delay, select_retry_server,
    DEFAULT_BASE_DELAY, DEFAULT_MAX_DELAY, DEFAULT_JITTER_FACTOR,
)


@pytest.mark.unit
class TestCalculateDelay:
    def test_exponential_backoff(self):
        """T-M3-05: delay = min(2 * 2^n, 60) ± 25% jitter."""
        delays = [calculate_delay(i, jitter_factor=0) for i in range(6)]
        assert delays[0] == pytest.approx(2.0)
        assert delays[1] == pytest.approx(4.0)
        assert delays[2] == pytest.approx(8.0)
        assert delays[3] == pytest.approx(16.0)
        assert delays[4] == pytest.approx(32.0)
        assert delays[5] == pytest.approx(60.0)

    def test_jitter_within_range(self):
        for _ in range(50):
            delay = calculate_delay(2, base_delay=2.0, jitter_factor=0.25)
            assert 6.0 <= delay <= 10.0

    def test_max_delay_cap(self):
        delay = calculate_delay(100, base_delay=2.0, max_delay=60.0, jitter_factor=0)
        assert delay == pytest.approx(60.0)


@pytest.mark.unit
class TestSelectRetryServer:
    def test_avoids_failed_server(self):
        """T-M3-06: retry selects different server."""
        for _ in range(20):
            selected = select_retry_server(["s1", "s2", "s3"], failed_server="s1")
            assert selected != "s1"

    def test_fallback_to_failed_if_only_option(self):
        selected = select_retry_server(["s1"], failed_server="s1")
        assert selected == "s1"

    def test_empty_list_returns_none(self):
        assert select_retry_server([], failed_server="s1") is None


@pytest.mark.unit
class TestRetryPolicy:
    def test_should_retry(self):
        """T-M3-07: 3 retries max then stop."""
        policy = RetryPolicy(max_retries=3)
        assert policy.should_retry(0) is True
        assert policy.should_retry(1) is True
        assert policy.should_retry(2) is True
        assert policy.should_retry(3) is False

    def test_get_delay(self):
        policy = RetryPolicy(base_delay=2.0, max_delay=60.0, jitter_factor=0)
        assert policy.get_delay(0) == pytest.approx(2.0)
        assert policy.get_delay(3) == pytest.approx(16.0)
