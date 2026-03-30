"""Application configuration via environment variables and .env files."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "ASR_", "env_file": ".env", "env_file_encoding": "utf-8"}

    app_name: str = "ASR Task Manager"
    app_version: str = "0.1.0"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    host: str = "0.0.0.0"
    port: int = 8000

    database_url: str = "sqlite+aiosqlite:///./data/asr_tasks.db"
    redis_url: str = "redis://localhost:6379/0"

    upload_dir: Path = Path("./data/uploads")
    result_dir: Path = Path("./data/results")
    temp_dir: Path = Path("./data/temp")
    max_upload_size_mb: int = 2048

    default_language: str = "zh"
    task_timeout_seconds: int = 3600
    max_retry_count: int = 3

    heartbeat_interval_seconds: int = 30
    heartbeat_timeout_seconds: int = 90

    auth_enabled: bool = False
    auth_tokens: str = ""

    cors_origins: str = "*"

    preprocess_fallback_enabled: bool = True

    rate_limit_enabled: bool = False
    rate_limit_max_concurrent: int = 10
    rate_limit_max_daily: int = 100
    rate_limit_max_upload_mb_per_min: int = 50


settings = Settings()
