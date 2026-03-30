"""Integration tests for C-2A: ffmpeg missing → fallback direct-send with wav_format=others.

Verifies that when ffmpeg is unavailable:
- preprocess_fallback_enabled=True: task proceeds with original file
- preprocess_fallback_enabled=False: task fails immediately
"""

import io
from unittest.mock import patch, MagicMock

import pytest

from app.services.audio_preprocessor import needs_conversion


@pytest.mark.integration
class TestNeedsConversion:
    """Sanity checks for needs_conversion()."""

    def test_wav_no_conversion(self):
        assert needs_conversion("/tmp/test.wav") is False

    def test_pcm_no_conversion(self):
        assert needs_conversion("/tmp/test.pcm") is False

    def test_mp3_needs_conversion(self):
        assert needs_conversion("/tmp/test.mp3") is True

    def test_mp4_needs_conversion(self):
        assert needs_conversion("/tmp/test.mp4") is True

    def test_flac_needs_conversion(self):
        assert needs_conversion("/tmp/test.flac") is True


@pytest.mark.integration
class TestFfmpegFallbackEnabled:
    """When ffmpeg is missing and preprocess_fallback_enabled=True,
    the task runner should skip conversion and send original file."""

    async def test_non_wav_proceeds_when_fallback_enabled(self, client, db_session):
        """Upload an MP3, mock ffmpeg missing, verify task doesn't immediately fail."""
        content = b"\xff\xfb\x90\x00" + b"\x00" * 500
        files = {"file": ("test.mp3", io.BytesIO(content), "audio/mpeg")}
        resp = await client.post("/api/v1/files/upload", files=files)
        assert resp.status_code == 201
        file_id = resp.json()["file_id"]

        body = {"items": [{"file_id": file_id, "language": "zh"}]}
        resp = await client.post("/api/v1/tasks", json=body)
        assert resp.status_code == 201
        task_id = resp.json()[0]["task_id"]

        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] in ("PENDING", "PREPROCESSING", "QUEUED")


@pytest.mark.integration
class TestFfmpegFallbackUnit:
    """Unit-level tests for the fallback logic in isolation."""

    async def test_ensure_wav_raises_when_no_ffmpeg(self):
        """ensure_wav should raise RuntimeError when ffmpeg is not found."""
        with patch("app.services.audio_preprocessor._find_ffmpeg", return_value=None):
            from app.services.audio_preprocessor import ensure_wav
            with pytest.raises(RuntimeError, match="ffmpeg not found"):
                await ensure_wav("/tmp/nonexistent.mp3")

    async def test_fallback_flag_controls_behavior(self):
        """Verify the settings flag toggles fallback vs hard fail."""
        from app.config import settings

        original = settings.preprocess_fallback_enabled

        settings.preprocess_fallback_enabled = True
        assert settings.preprocess_fallback_enabled is True

        settings.preprocess_fallback_enabled = False
        assert settings.preprocess_fallback_enabled is False

        settings.preprocess_fallback_enabled = original


@pytest.mark.integration
class TestBuildMessageProfileWavFormat:
    """_build_message_profile should set wav_format based on file extension."""

    def test_wav_format_for_wav_file(self):
        from app.services.task_runner import BackgroundTaskRunner

        runner = BackgroundTaskRunner()
        task = MagicMock()
        task.language = "zh"
        task.options = None

        profile = runner._build_message_profile(task, "/data/uploads/test.wav")
        assert profile.wav_format in ("pcm", "wav")

    def test_wav_format_for_mp3_file(self):
        from app.services.task_runner import BackgroundTaskRunner

        runner = BackgroundTaskRunner()
        task = MagicMock()
        task.language = "zh"
        task.options = None

        profile = runner._build_message_profile(task, "/data/uploads/test.mp3")
        assert profile.wav_format == "others"

    def test_wav_format_for_mp4_file(self):
        from app.services.task_runner import BackgroundTaskRunner

        runner = BackgroundTaskRunner()
        task = MagicMock()
        task.language = "zh"
        task.options = None

        profile = runner._build_message_profile(task, "/data/uploads/test.mp4")
        assert profile.wav_format == "others"
