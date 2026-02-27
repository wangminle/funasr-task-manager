"""File cleanup service unit tests."""

import time
from pathlib import Path

import pytest

from app.services.cleanup import CleanupService


@pytest.mark.unit
class TestCleanupService:
    def test_cleanup_empty_dir(self, tmp_path):
        svc = CleanupService()
        count = svc._cleanup_dir(tmp_path, max_age_seconds=0)
        assert count == 0

    def test_cleanup_old_files(self, tmp_path):
        prefix = tmp_path / "01AB"
        prefix.mkdir()
        old_dir = prefix / "old-file-id"
        old_dir.mkdir()
        (old_dir / "test.wav").write_bytes(b"data")
        import os
        old_time = time.time() - 100
        os.utime(old_dir, (old_time, old_time))

        svc = CleanupService()
        count = svc._cleanup_dir(tmp_path, max_age_seconds=50)
        assert count == 1
        assert not old_dir.exists()

    def test_cleanup_skips_active_ids(self, tmp_path):
        prefix = tmp_path / "01AB"
        prefix.mkdir()
        active = prefix / "active-id"
        active.mkdir()
        (active / "test.wav").write_bytes(b"data")
        import os
        old_time = time.time() - 100
        os.utime(active, (old_time, old_time))

        svc = CleanupService()
        count = svc._cleanup_dir(tmp_path, max_age_seconds=50, active_ids={"active-id"})
        assert count == 0
        assert active.exists()

    def test_cleanup_nonexistent_dir(self, tmp_path):
        svc = CleanupService()
        count = svc._cleanup_dir(tmp_path / "nonexistent", max_age_seconds=0)
        assert count == 0
