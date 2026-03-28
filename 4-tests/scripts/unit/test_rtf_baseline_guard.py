"""Unit tests for RTF baseline persist guard (Bug P1 regression fix).

Verifies that _persist_rtf_baseline is NOT called when the RTF tracker
has fewer than 3 samples, preventing benchmark RTF from being overwritten
with DEFAULT_RTF during cold start.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.scheduler import DEFAULT_RTF, TaskScheduler


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
    """Verify the task_runner guard: only persist when window_size >= 3.

    This is the core regression test for Bug P1.
    """

    def test_persist_skipped_on_first_completion(self):
        """After 1st task completion, persist should NOT be called."""
        from app.services.task_runner import BackgroundTaskRunner
        runner = BackgroundTaskRunner()

        with patch.object(runner, '_persist_rtf_baseline', new_callable=AsyncMock) as mock_persist:
            scheduler = TaskScheduler()
            with patch('app.services.task_runner.global_scheduler', scheduler):
                scheduler.rtf_tracker.record("srv-1", 0.124)
                window = scheduler.rtf_tracker.get_window_size("srv-1")
                assert window == 1
                assert window < 3

    def test_persist_called_on_third_completion(self):
        """After 3rd task completion, persist SHOULD be called."""
        scheduler = TaskScheduler()
        for _ in range(3):
            scheduler.rtf_tracker.record("srv-1", 0.124)
        assert scheduler.rtf_tracker.get_window_size("srv-1") == 3
        p90 = scheduler.rtf_tracker.get_p90("srv-1")
        assert p90 != DEFAULT_RTF

    def test_benchmark_rtf_not_overwritten_during_cold_start(self):
        """Simulate the exact bug scenario:

        1. Server has benchmark RTF = 0.124
        2. First task completes → calibrate returns DEFAULT_RTF (0.3)
        3. Guard prevents 0.3 from being written to DB
        4. Original 0.124 is preserved
        """
        scheduler = TaskScheduler()

        result = scheduler.calibrate_after_completion(
            server_id="srv-benchmark",
            audio_duration_sec=60.0,
            actual_duration_sec=7.44,
        )
        assert result["new_rtf_p90"] == DEFAULT_RTF

        window_size = scheduler.rtf_tracker.get_window_size("srv-benchmark")
        assert window_size == 1
        assert window_size < 3

    def test_real_rtf_persisted_after_sufficient_samples(self):
        """After enough samples, real RTF should be used."""
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
