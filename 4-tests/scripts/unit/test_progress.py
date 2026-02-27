"""Two-layer progress calculation tests."""

from datetime import datetime, timezone, timedelta

import pytest

from app.services.progress import calculate_progress, calculate_eta, format_progress_message


@pytest.mark.unit
class TestCalculateProgress:
    def test_pending_returns_zero(self):
        assert calculate_progress("PENDING") == pytest.approx(0.0)

    def test_preprocessing_returns_5_percent(self):
        assert calculate_progress("PREPROCESSING") == pytest.approx(0.05)

    def test_queued_returns_15_percent(self):
        assert calculate_progress("QUEUED") == pytest.approx(0.15)

    def test_succeeded_returns_100_percent(self):
        assert calculate_progress("SUCCEEDED") == pytest.approx(1.0)

    def test_transcribing_with_elapsed_time(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=15)
        progress = calculate_progress("TRANSCRIBING", started_at=started, duration_sec=100, rtf_p90=0.3)
        assert 0.50 < progress < 0.65

    def test_transcribing_without_started_at(self):
        assert calculate_progress("TRANSCRIBING") == pytest.approx(0.20)

    def test_transcribing_capped_at_95_percent(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=1000)
        progress = calculate_progress("TRANSCRIBING", started_at=started, duration_sec=10, rtf_p90=0.3)
        assert progress <= 0.95


@pytest.mark.unit
class TestCalculateEta:
    def test_succeeded_returns_zero(self):
        assert calculate_eta("SUCCEEDED") == 0

    def test_transcribing_with_duration(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=10)
        eta = calculate_eta("TRANSCRIBING", started_at=started, duration_sec=100, rtf_p90=0.3)
        assert eta is not None
        assert 15 <= eta <= 25

    def test_pending_with_duration(self):
        eta = calculate_eta("PENDING", duration_sec=60, rtf_p90=0.3)
        assert eta is not None
        assert eta == int(60 * 0.3 + 5)


@pytest.mark.unit
class TestFormatProgressMessage:
    def test_pending_message(self):
        assert "等待" in format_progress_message("PENDING", 0.0)

    def test_transcribing_message(self):
        msg = format_progress_message("TRANSCRIBING", 0.5)
        assert "转写中" in msg
        assert "50%" in msg

    def test_queued_with_position(self):
        msg = format_progress_message("QUEUED", 0.15, queue_position=3)
        assert "第3位" in msg
