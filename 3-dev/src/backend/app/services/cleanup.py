"""File cleanup service - remove expired uploads, temp files, and old results."""

import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_UPLOAD_TTL_DAYS = 30
DEFAULT_RESULT_TTL_DAYS = 90
DEFAULT_TEMP_TTL_HOURS = 24


class CleanupService:
    """Periodic file cleanup service."""

    def __init__(
        self,
        upload_ttl_days: int = DEFAULT_UPLOAD_TTL_DAYS,
        result_ttl_days: int = DEFAULT_RESULT_TTL_DAYS,
        temp_ttl_hours: int = DEFAULT_TEMP_TTL_HOURS,
    ):
        self.upload_ttl_days = upload_ttl_days
        self.result_ttl_days = result_ttl_days
        self.temp_ttl_hours = temp_ttl_hours

    def cleanup_temp_files(self) -> int:
        """Remove temp files older than temp_ttl_hours."""
        return self._cleanup_dir(
            settings.temp_dir,
            max_age_seconds=self.temp_ttl_hours * 3600,
        )

    def cleanup_old_uploads(self, active_file_ids: set[str] | None = None) -> int:
        """Remove upload directories older than upload_ttl_days that are not active."""
        return self._cleanup_dir(
            settings.upload_dir,
            max_age_seconds=self.upload_ttl_days * 86400,
            active_ids=active_file_ids,
        )

    def cleanup_old_results(self, active_task_ids: set[str] | None = None) -> int:
        """Remove result directories older than result_ttl_days."""
        return self._cleanup_dir(
            settings.result_dir,
            max_age_seconds=self.result_ttl_days * 86400,
            active_ids=active_task_ids,
        )

    def run_all(self, active_file_ids: set[str] | None = None, active_task_ids: set[str] | None = None) -> dict:
        """Run all cleanup tasks. Returns counts of deleted items."""
        temp_count = self.cleanup_temp_files()
        upload_count = self.cleanup_old_uploads(active_file_ids)
        result_count = self.cleanup_old_results(active_task_ids)
        total = temp_count + upload_count + result_count
        if total > 0:
            logger.info("cleanup_completed", temp=temp_count, uploads=upload_count, results=result_count)
        return {"temp": temp_count, "uploads": upload_count, "results": result_count}

    def _cleanup_dir(self, base_dir: Path, max_age_seconds: int, active_ids: set[str] | None = None) -> int:
        """Remove subdirectories older than max_age_seconds."""
        if not base_dir.exists():
            return 0
        count = 0
        now = time.time()
        cutoff = now - max_age_seconds
        for prefix_dir in base_dir.iterdir():
            if not prefix_dir.is_dir():
                continue
            for item_dir in prefix_dir.iterdir():
                if not item_dir.is_dir():
                    continue
                if active_ids and item_dir.name in active_ids:
                    continue
                try:
                    mtime = item_dir.stat().st_mtime
                    if mtime < cutoff:
                        shutil.rmtree(item_dir)
                        count += 1
                except Exception as e:
                    logger.warning("cleanup_error", path=str(item_dir), error=str(e))
            if prefix_dir.is_dir() and not any(prefix_dir.iterdir()):
                try:
                    prefix_dir.rmdir()
                except Exception:
                    pass
        return count


cleanup_service = CleanupService()
