"""File storage management."""

import shutil
from pathlib import Path

import aiofiles

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".mp4", ".flac", ".ogg", ".webm", ".m4a", ".aac", ".wma", ".mkv", ".avi", ".mov", ".pcm"}


def validate_file_extension(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in ALLOWED_EXTENSIONS


def get_upload_path(file_id: str, original_name: str) -> Path:
    prefix = file_id[:4]
    target_dir = settings.upload_dir / prefix / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / original_name


def get_result_path(task_id: str, fmt: str = "json") -> Path:
    prefix = task_id[:4]
    target_dir = settings.result_dir / prefix / task_id
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"result.{fmt}"


async def save_upload(file_id: str, original_name: str, content: bytes) -> Path:
    path = get_upload_path(file_id, original_name)
    async with aiofiles.open(path, "wb") as f:
        await f.write(content)
    logger.info("file_saved", file_id=file_id, path=str(path), size=len(content))
    return path


async def save_result(task_id: str, content: str, fmt: str = "json") -> Path:
    path = get_result_path(task_id, fmt)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(content)
    return path


async def read_result(task_id: str, fmt: str = "json") -> str | None:
    path = get_result_path(task_id, fmt)
    if not path.exists():
        return None
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


def delete_file(file_id: str) -> bool:
    prefix = file_id[:4]
    target_dir = settings.upload_dir / prefix / file_id
    if target_dir.exists():
        shutil.rmtree(target_dir)
        logger.info("file_deleted", file_id=file_id)
        return True
    return False
