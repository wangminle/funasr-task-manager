"""File storage management."""

import asyncio
import shutil
from pathlib import Path

import aiofiles

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

ALLOWED_EXTENSIONS = {".wav", ".mp3", ".mp4", ".flac", ".ogg", ".webm", ".m4a", ".aac", ".wma", ".mkv", ".avi", ".mov", ".pcm"}


def sanitize_filename(original_name: str) -> str:
    """Strip path separators and traversal components, keeping only the basename."""
    safe_name = Path(original_name).name
    safe_name = safe_name.lstrip(".")
    if not safe_name:
        safe_name = "unnamed"
    return safe_name


def validate_file_extension(filename: str) -> bool:
    safe = sanitize_filename(filename)
    ext = Path(safe).suffix.lower()
    return ext in ALLOWED_EXTENSIONS


def get_upload_path(file_id: str, original_name: str) -> Path:
    safe_name = sanitize_filename(original_name)
    prefix = file_id[:4]
    target_dir = settings.upload_dir / prefix / file_id
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = (target_dir / safe_name).resolve()
    if not str(final_path).startswith(str(target_dir.resolve())):
        raise ValueError(f"Path traversal detected in filename: {original_name}")
    return final_path


def get_result_dir(task_id: str) -> Path:
    prefix = task_id[:4]
    return settings.result_dir / prefix / task_id


def get_result_path(task_id: str, fmt: str = "json") -> Path:
    target_dir = get_result_dir(task_id)
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
    path = get_result_dir(task_id) / f"result.{fmt}"
    exists = await asyncio.to_thread(path.exists)
    if not exists:
        return None
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return await f.read()


async def delete_file(file_id: str) -> bool:
    prefix = file_id[:4]
    target_dir = settings.upload_dir / prefix / file_id
    exists = await asyncio.to_thread(target_dir.exists)
    if exists:
        await asyncio.to_thread(shutil.rmtree, target_dir)
        logger.info("file_deleted", file_id=file_id)
        return True
    return False


async def delete_result(task_id: str) -> bool:
    target_dir = get_result_dir(task_id)
    exists = await asyncio.to_thread(target_dir.exists)
    if exists:
        await asyncio.to_thread(shutil.rmtree, target_dir)
        logger.info("result_deleted", task_id=task_id)
        return True
    return False
