"""Application configuration via environment variables and .env files."""

import os
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


def _detect_project_root() -> Path:
    """Detect the repository / project root directory.

    Local dev layout:  <repo>/3-dev/src/backend/app/config.py  → parents[4]
    Docker layout:     /app/app/config.py                      → parents[1]

    Set ASR_PROJECT_ROOT to override auto-detection in any environment.
    """
    env_val = os.environ.get("ASR_PROJECT_ROOT")
    if env_val:
        return Path(env_val).resolve()
    try:
        return Path(__file__).resolve().parents[4]
    except IndexError:
        return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _detect_project_root()
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "runtime"
DEFAULT_STORAGE_ROOT = DEFAULT_RUNTIME_ROOT / "storage"
DEFAULT_LOGS_DIR = DEFAULT_RUNTIME_ROOT / "logs"


def _resolve_project_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def _sqlite_url_for_path(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _normalize_sqlite_url(url: str) -> str:
    scheme = "sqlite+aiosqlite:///"
    if not url.startswith(scheme):
        return url
    raw_path = url[len(scheme):]
    if not raw_path:
        return url
    if Path(raw_path).is_absolute():
        return url
    normalized_path = _resolve_project_path(raw_path)
    return f"{scheme}{normalized_path.as_posix()}"


class Settings(BaseSettings):
    model_config = {"env_prefix": "ASR_", "env_file": ".env", "env_file_encoding": "utf-8"}

    app_name: str = "ASR Task Manager"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    host: str = "0.0.0.0"
    port: int = 8000

    runtime_root: Path = DEFAULT_RUNTIME_ROOT
    storage_root: Path | None = None
    logs_dir: Path | None = None
    database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    upload_dir: Path | None = None
    result_dir: Path | None = None
    temp_dir: Path | None = None
    max_upload_size_mb: int = 2048

    default_language: str = "zh"
    task_timeout_seconds: int = 3600
    max_retry_count: int = 3

    heartbeat_interval_seconds: int = 30
    heartbeat_timeout_seconds: int = 90

    auth_enabled: bool = False
    auth_tokens: str = ""
    ssrf_protection_enabled: bool = False

    cors_origins: str = ""

    preprocess_fallback_enabled: bool = True

    # --- VAD segmentation for long audio parallel transcription ---
    segment_enabled: bool = True
    segment_min_file_duration_sec: int = 600
    segment_target_duration_sec: int = 600
    segment_min_duration_sec: int = 120
    segment_max_duration_sec: int = 780
    segment_overlap_ms: int = 400
    segment_silence_noise_db: int = -35
    segment_silence_min_duration: float = 0.8
    segment_search_step_sec: int = 60
    segment_search_max_rounds: int = 3
    segment_fallback_silence_sec: float = 0.3
    segment_max_parallel_per_task: int = 3
    segment_max_retry_count: int = 3

    rate_limit_enabled: bool = False
    rate_limit_max_concurrent: int = 10
    rate_limit_max_daily: int = 100
    rate_limit_max_upload_mb_per_min: int = 50

    def model_post_init(self, __context) -> None:
        runtime_root = _resolve_project_path(self.runtime_root)
        storage_root = _resolve_project_path(self.storage_root or (runtime_root / "storage"))
        logs_dir = _resolve_project_path(self.logs_dir or (runtime_root / "logs"))
        upload_dir = _resolve_project_path(self.upload_dir or (storage_root / "uploads"))
        result_dir = _resolve_project_path(self.result_dir or (storage_root / "results"))
        temp_dir = _resolve_project_path(self.temp_dir or (storage_root / "temp"))
        database_url = _normalize_sqlite_url(self.database_url) if self.database_url else _sqlite_url_for_path(storage_root / "asr_tasks.db")

        object.__setattr__(self, "runtime_root", runtime_root)
        object.__setattr__(self, "storage_root", storage_root)
        object.__setattr__(self, "logs_dir", logs_dir)
        object.__setattr__(self, "upload_dir", upload_dir)
        object.__setattr__(self, "result_dir", result_dir)
        object.__setattr__(self, "temp_dir", temp_dir)
        object.__setattr__(self, "database_url", database_url)


settings = Settings()
