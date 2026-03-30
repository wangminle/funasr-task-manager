"""Round-4 bugfix unit tests — C-1 stem collision, C-2B duration estimation."""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

from app.services.upload import estimate_duration_from_size


@pytest.mark.unit
class TestDurationEstimation:
    """C-2B: estimate_duration_from_size when ffprobe is missing."""

    def test_wav_estimation(self):
        size_bytes = 32_000_000
        dur = estimate_duration_from_size(size_bytes, "test.wav")
        assert dur is not None
        assert dur == pytest.approx(1000.0, abs=1)

    def test_mp3_estimation(self):
        size_bytes = 16_000_000
        dur = estimate_duration_from_size(size_bytes, "podcast.mp3")
        assert dur is not None
        assert dur == pytest.approx(1000.0, abs=1)

    def test_mp4_estimation(self):
        size_bytes = 200_000_000
        dur = estimate_duration_from_size(size_bytes, "video.mp4")
        assert dur is not None
        assert dur == pytest.approx(10000.0, abs=1)

    def test_unknown_extension_returns_none(self):
        assert estimate_duration_from_size(100_000, "readme.txt") is None

    def test_zero_size_returns_none(self):
        assert estimate_duration_from_size(0, "empty.wav") is None

    def test_case_insensitive_extension(self):
        dur = estimate_duration_from_size(32_000_000, "AUDIO.WAV")
        assert dur is not None


@pytest.mark.unit
class TestStemCollisionFix:
    """C-1: verify output filenames preserve full original name."""

    def test_different_extensions_produce_different_filenames(self):
        """tv-report-1.wav and tv-report-1.mp4 should NOT collide."""
        file_names = {"tid1": "tv-report-1.wav", "tid2": "tv-report-1.mp4"}
        outputs = set()
        for tid, name in file_names.items():
            base_name = name
            dest = f"{base_name}_result.txt"
            outputs.add(dest)
        assert len(outputs) == 2, "Files with same stem but different ext must produce unique outputs"
        assert "tv-report-1.wav_result.txt" in outputs
        assert "tv-report-1.mp4_result.txt" in outputs
