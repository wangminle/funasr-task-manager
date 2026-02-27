"""File upload service - handles upload processing and metadata extraction."""

from ulid import ULID

from app.models import File
from app.services.metadata import extract_metadata
from app.storage.file_manager import save_upload, validate_file_extension
from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)


class UploadError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def process_upload(user_id: str, filename: str, content: bytes) -> File:
    if not validate_file_extension(filename):
        raise UploadError(f"Unsupported file format: {filename}", 400)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise UploadError(f"File too large: {len(content)} bytes (max {settings.max_upload_size_mb}MB)", 413)
    file_id = str(ULID())
    storage_path = await save_upload(file_id, filename, content)
    file_record = File(
        file_id=file_id, user_id=user_id, original_name=filename,
        size_bytes=len(content), storage_path=str(storage_path), status="UPLOADED",
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
