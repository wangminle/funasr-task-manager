"""Pydantic schemas for SSE progress events."""

from datetime import datetime

from pydantic import BaseModel


class ProgressEvent(BaseModel):
    task_id: str
    event_type: str
    progress: float
    eta_seconds: int | None = None
    message: str
    timestamp: datetime
