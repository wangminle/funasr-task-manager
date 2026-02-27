"""File upload service - handles upload processing and metadata extraction."""

from typing import BinaryIO, Protocol

import aiofiles
from ulid import ULID

from app.models import File
from app.services.metadata import extract_metadata
from app.storage.file_manager import get_upload_path, save_upload, validate_file_extension
from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

STREAM_CHUNK_SIZE = 256 * 1024


class UploadError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def process_upload(user_id: str, filename: str, content: bytes) -> File:
    """Legacy in-memory upload (kept for backward compatibility with tests)."""
    if not validate_file_extension(filename):
        raise UploadError(f"Unsupported file format: {filename}", 400)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise UploadError(f"File too large: {len(content)} bytes (max {settings.max_upload_size_mb}MB)", 413)
    file_id = str(ULID())
    storage_path = await save_upload(file_id, filename, content)
    return await _build_file_record(file_id, user_id, filename, len(content), storage_path)


async def process_upload_streaming(user_id: str, filename: str, upload_file, max_bytes: int) -> File:
    """Stream-based upload: reads file in chunks to avoid loading the entire file into memory."""
    if not validate_file_extension(filename):
        raise UploadError(f"Unsupported file format: {filename}", 400)

    file_id = str(ULID())
    dest_path = get_upload_path(file_id, filename)
    total_bytes = 0

    try:
        async with aiofiles.open(dest_path, "wb") as out:
            while True:
                chunk = await upload_file.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise UploadError(
                        f"File too large: exceeds {max_bytes // (1024 * 1024)}MB limit", 413
                    )
                await out.write(chunk)
    except UploadError:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        raise UploadError(f"Upload I/O error: {exc}", 500) from exc

    logger.info("file_saved", file_id=file_id, path=str(dest_path), size=total_bytes)
    return await _build_file_record(file_id, user_id, filename, total_bytes, dest_path)


async def _build_file_record(file_id, user_id, filename, size_bytes, storage_path) -> File:
    file_record = File(
        file_id=file_id, user_id=user_id, original_name=filename,
        size_bytes=size_bytes, storage_path=str(storage_path), status="UPLOADED",
    )
    meta = await extract_metadata(storage_path)
    if meta.error is None:
        file_record.media_type = meta.media_type
        file_record.mime = meta.mime
        file_record.duration_sec = meta.duration_sec
        file_record.codec = meta.codec
        file_record.sample_rate = meta.sample_rate
        file_record.channels = meta.channels
        file_record.status = "META_READY"
    else:
        logger.warning("metadata_extraction_failed", file_id=file_id, error=meta.error)
    return file_record
