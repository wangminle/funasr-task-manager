"""Pydantic schemas for file upload and metadata."""

from datetime import datetime

from pydantic import BaseModel


class FileUploadResponse(BaseModel):
    file_id: str
    original_name: str
    size_bytes: int
    status: str
    created_at: datetime


class FileMetadataResponse(BaseModel):
    file_id: str
    user_id: str
    original_name: str
    media_type: str | None = None
    mime: str | None = None
    duration_sec: float | None = None
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    size_bytes: int
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
