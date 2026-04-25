"""Pydantic schemas for task management."""

from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, Field, model_validator


class AutoSegmentMode(StrEnum):
    AUTO = "auto"
    ON = "on"
    OFF = "off"


class CallbackConfig(BaseModel):
    url: str
    secret: str | None = None


class TaskItemCreate(BaseModel):
    file_id: str
    language: str = "zh"
    options: dict | None = None


class TaskCreateRequest(BaseModel):
    items: list[TaskItemCreate] = Field(..., min_length=1, max_length=100)
    callback: CallbackConfig | None = None
    auto_segment: AutoSegmentMode = AutoSegmentMode.AUTO


class SegmentSummary(BaseModel):
    """Diagnostic info about VAD-based parallel segments for a task."""
    total: int
    succeeded: int = 0
    failed: int = 0
    pending: int = 0
    active: int = 0
    assigned_server_ids: list[str] = []


class TaskResponse(BaseModel):
    task_id: str
    user_id: str
    file_id: str
    file_name: str | None = None
    task_group_id: str | None = None
    status: str
    progress: float
    eta_seconds: int | None = None
    language: str
    assigned_server_id: str | None = None
    result_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_count: int = 0
    is_terminal: bool = False
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    segment_info: SegmentSummary | None = Field(None, serialization_alias="segments")

    model_config = {"from_attributes": True, "populate_by_name": True}

    @model_validator(mode="after")
    def _compute_is_terminal(self) -> "TaskResponse":
        if self.status in ("SUCCEEDED", "CANCELED"):
            self.is_terminal = True
        elif self.status == "FAILED":
            from app.config import settings
            self.is_terminal = self.retry_count >= settings.max_retry_count
        return self


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int
    page: int
    page_size: int
