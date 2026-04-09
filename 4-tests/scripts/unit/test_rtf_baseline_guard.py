"""Unit tests for RTF baseline integrity and degradation detection.

Design invariant (P1): rtf_baseline is set exclusively by the benchmark
service.  The task runner updates the in-memory rolling window (for ETA
estimation) but never writes back to the database column.

Degradation detection (P0/P3): benchmark gradient tests always cover
(1,2,4,8) and use _detect_optimal_concurrency to find the highest
non-degraded concurrency level for setting max_concurrency.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduler import DEFAULT_RTF, TaskScheduler
from app.services.server_benchmark import (
    ConcurrencyGradient,
    _detect_optimal_concurrency,
    THROUGHPUT_MIN_IMPROVEMENT,
    PER_FILE_MAX_DEGRADATION,
)


@pytest.mark.unit
class TestRTFTrackerWindowGuard:
    """Verify RTFTracker.get_p90 returns DEFAULT_RTF when samples < 3."""

    def test_get_p90_with_zero_samples(self):
        scheduler = TaskScheduler()
        assert scheduler.rtf_tracker.get_p90("srv-1") == DEFAULT_RTF

    def test_get_p90_with_one_sample(self):
        scheduler = TaskScheduler()
        scheduler.rtf_tracker.record("srv-1", 0.124)
        assert scheduler.rtf_tracker.get_p90("srv-1") == DEFAULT_RTF
        assert scheduler.rtf_tracker.get_window_size("srv-1") == 1

    def test_get_p90_with_two_samples(self):
        scheduler = TaskScheduler()
        scheduler.rtf_tracker.record("srv-1", 0.124)
        scheduler.rtf_tracker.record("srv-1", 0.131)
        assert scheduler.rtf_tracker.get_p90("srv-1") == DEFAULT_RTF
        assert scheduler.rtf_tracker.get_window_size("srv-1") == 2

    def test_get_p90_with_three_samples_returns_real_value(self):
        scheduler = TaskScheduler()
        scheduler.rtf_tracker.record("srv-1", 0.10)
        scheduler.rtf_tracker.record("srv-1", 0.12)
        scheduler.rtf_tracker.record("srv-1", 0.14)
        p90 = scheduler.rtf_tracker.get_p90("srv-1")
        assert p90 != DEFAULT_RTF
        assert 0.10 <= p90 <= 0.14
        assert scheduler.rtf_tracker.get_window_size("srv-1") == 3


@pytest.mark.unit
class TestCalibrateAfterCompletion:
    """Verify calibrate_after_completion returns DEFAULT_RTF when samples < 3."""

    def test_first_completion_returns_default_rtf(self):
        scheduler = TaskScheduler()
        result = scheduler.calibrate_after_completion(
            server_id="srv-1",
            audio_duration_sec=60.0,
            actual_duration_sec=7.44,
        )
        assert result["new_rtf_p90"] == DEFAULT_RTF

    def test_third_completion_returns_real_rtf(self):
        scheduler = TaskScheduler()
        for _ in range(2):
            scheduler.calibrate_after_completion(
                server_id="srv-1",
                audio_duration_sec=60.0,
                actual_duration_sec=7.44,
            )
        result = scheduler.calibrate_after_completion(
            server_id="srv-1",
            audio_duration_sec=60.0,
            actual_duration_sec=7.44,
        )
        assert result["new_rtf_p90"] != DEFAULT_RTF
        assert abs(result["new_rtf_p90"] - 0.124) < 0.01


@pytest.mark.unit
class TestPersistRTFBaselineGuard:
    """Verify that the task_runner no longer persists rtf_baseline.

    Design: rtf_baseline is benchmark-only; the task runner only feeds
    the in-memory rolling window for ETA estimation.
    """

    def test_task_runner_has_no_persist_method(self):
        """BackgroundTaskRunner must NOT have _persist_rtf_baseline."""
        from app.services.task_runner import BackgroundTaskRunner
        runner = BackgroundTaskRunner()
        assert not hasattr(runner, '_persist_rtf_baseline'), (
            "_persist_rtf_baseline should have been removed — "
            "rtf_baseline is now set exclusively by the benchmark service"
        )

    def test_rolling_window_still_updated_after_calibration(self):
        """calibrate_after_completion should still feed the rolling window."""
        scheduler = TaskScheduler()
        for _ in range(3):
            scheduler.calibrate_after_completion(
                server_id="srv-1",
                audio_duration_sec=60.0,
                actual_duration_sec=7.44,
            )
        assert scheduler.rtf_tracker.get_window_size("srv-1") == 3
        p90 = scheduler.rtf_tracker.get_p90("srv-1")
        assert p90 != DEFAULT_RTF

    def test_benchmark_rtf_not_overwritten_during_cold_start(self):
        """First calibration returns DEFAULT_RTF but that's fine — it only
        affects the in-memory rolling window, not the DB column.
        """
        scheduler = TaskScheduler()
        result = scheduler.calibrate_after_completion(
            server_id="srv-benchmark",
            audio_duration_sec=60.0,
            actual_duration_sec=7.44,
        )
        assert result["new_rtf_p90"] == DEFAULT_RTF
        assert scheduler.rtf_tracker.get_window_size("srv-benchmark") == 1

    def test_real_p90_after_sufficient_samples(self):
        """After enough samples the rolling window returns real RTF."""
        scheduler = TaskScheduler()
        for actual_sec in [7.44, 7.86, 8.40]:
            scheduler.calibrate_after_completion(
                server_id="srv-benchmark",
                audio_duration_sec=60.0,
                actual_duration_sec=actual_sec,
            )

        window_size = scheduler.rtf_tracker.get_window_size("srv-benchmark")
        assert window_size == 3
        p90 = scheduler.rtf_tracker.get_p90("srv-benchmark")
        assert 0.12 <= p90 <= 0.15
        assert p90 != DEFAULT_RTF


def _g(concurrency, per_file_rtf, throughput_rtf, wall_clock_sec=1.0, total_audio_sec=3.0):
    """Helper to build ConcurrencyGradient instances for tests."""
    return ConcurrencyGradient(
        concurrency=concurrency,
        per_file_rtf=per_file_rtf,
        throughput_rtf=throughput_rtf,
        wall_clock_sec=wall_clock_sec,
        total_audio_sec=total_audio_sec,
    )


@pytest.mark.unit
class TestDetectOptimalConcurrency:
    """Verify the degradation detection algorithm."""

    def test_ideal_8_worker_server(self):
        """8 workers: throughput improves at every level → recommended=8."""
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.025, 0.0125),
            _g(4, 0.026, 0.0065),
            _g(8, 0.028, 0.0035),
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 8
        assert tp == 0.0035

    def test_2_worker_server_claiming_4(self):
        """Server has 2 real workers but claims 4.
        N=4 shows no throughput improvement over N=2 → recommended=2.
        """
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.025, 0.0125),
            _g(4, 0.050, 0.0125),   # no improvement: 0.0125 == 0.0125
            _g(8, 0.100, 0.0125),
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 2
        assert tp == 0.0125

    def test_4_worker_server_saturates_at_8(self):
        """4 workers: good up to N=4, saturated at N=8 → recommended=4."""
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.025, 0.0125),
            _g(4, 0.026, 0.0065),
            _g(8, 0.052, 0.0065),   # no improvement
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 4
        assert tp == 0.0065

    def test_degradation_at_high_concurrency(self):
        """Throughput actually worsens at N=8 → recommended=4."""
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.025, 0.0125),
            _g(4, 0.027, 0.00675),
            _g(8, 0.080, 0.010),    # regression: 0.010 > 0.00675
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 4
        assert tp == 0.00675

    def test_per_file_rtf_too_high(self):
        """per_file_rtf exceeds 2× single_rtf → degrade that level."""
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.025, 0.0125),
            _g(4, 0.060, 0.015),    # per_file_rtf 0.060 > 2 × 0.024 = 0.048
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 2
        assert tp == 0.0125

    def test_single_level_gradient(self):
        """Only N=1 tested → recommended=1."""
        gradient = [_g(1, 0.024, 0.024)]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 1
        assert tp == 0.024

    def test_empty_gradient(self):
        """Empty gradient → fallback to 1."""
        n, tp = _detect_optimal_concurrency([], single_rtf=0.024)
        assert n == 1
        assert tp == 0.024

    def test_no_single_rtf_skips_per_file_check(self):
        """When single_rtf is None, only throughput improvement is checked."""
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.100, 0.0125),   # per_file high but no single_rtf to compare
            _g(4, 0.200, 0.0065),
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=None)
        assert n == 4
        assert tp == 0.0065

    def test_marginal_improvement_below_threshold(self):
        """Improvement just below 10% threshold → rejected."""
        gradient = [
            _g(1, 0.024, 0.024),
            _g(2, 0.025, 0.0125),
            _g(4, 0.027, 0.01138),  # improvement = 1 - 0.01138/0.0125 = 8.96% < 10%
        ]
        n, tp = _detect_optimal_concurrency(gradient, single_rtf=0.024)
        assert n == 2
        assert tp == 0.0125
