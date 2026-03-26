"""File upload and metadata query endpoints."""

from fastapi import APIRouter, HTTPException, UploadFile
from sqlalchemy import select

from app.auth.rate_limiter import rate_limiter
from app.deps import CurrentUser, DbSession
from app.models import File
from app.schemas.file import FileMetadataResponse, FileUploadResponse
from app.services.upload import UploadError, process_upload_streaming
from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/files", tags=["files"])

UPLOAD_CHUNK_SIZE = 256 * 1024  # 256 KB per chunk


@router.post("/upload", response_model=FileUploadResponse, status_code=201)
async def upload_file(file: UploadFile, db: DbSession, user_id: CurrentUser):
    filename = file.filename or "unknown"
    content_length = file.size or 0
    rate_limiter.check_upload_bandwidth(user_id, content_length)
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    try:
        file_record = await process_upload_streaming(user_id, filename, file, max_bytes)
    except UploadError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    db.add(file_record)
    await db.flush()
    rate_limiter.record_upload(user_id, file_record.size_bytes)
    logger.info("file_uploaded", file_id=file_record.file_id, name=filename, size=file_record.size_bytes)
    return FileUploadResponse(
        file_id=file_record.file_id, original_name=file_record.original_name,
        size_bytes=file_record.size_bytes, status=file_record.status, created_at=file_record.created_at,
    )


@router.get("/{file_id}", response_model=FileMetadataResponse)
async def get_file_metadata(file_id: str, db: DbSession, user_id: CurrentUser):
    result = await db.execute(select(File).where(File.file_id == file_id, File.user_id == user_id))
    file_record = result.scalar_one_or_none()
    if file_record is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileMetadataResponse.model_validate(file_record)
