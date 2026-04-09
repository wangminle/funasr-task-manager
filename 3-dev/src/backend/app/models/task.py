"""Task model - ASR transcription task lifecycle."""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    PREPROCESSING = "PREPROCESSING"
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    TRANSCRIBING = "TRANSCRIBING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.PREPROCESSING, TaskStatus.CANCELED},
    TaskStatus.PREPROCESSING: {TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELED},
    TaskStatus.QUEUED: {TaskStatus.DISPATCHED, TaskStatus.CANCELED, TaskStatus.FAILED},
    TaskStatus.DISPATCHED: {TaskStatus.TRANSCRIBING, TaskStatus.FAILED, TaskStatus.CANCELED},
    TaskStatus.TRANSCRIBING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: {TaskStatus.QUEUED},
    TaskStatus.CANCELED: set(),
}

STATUS_PROGRESS_RANGES: dict[TaskStatus, tuple[float, float]] = {
    TaskStatus.PENDING: (0.0, 0.05),
    TaskStatus.PREPROCESSING: (0.05, 0.15),
    TaskStatus.QUEUED: (0.15, 0.20),
    TaskStatus.DISPATCHED: (0.20, 0.20),
    TaskStatus.TRANSCRIBING: (0.20, 0.95),
    TaskStatus.SUCCEEDED: (1.0, 1.0),
    TaskStatus.FAILED: (0.0, 1.0),
    TaskStatus.CANCELED: (0.0, 1.0),
}


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_id: Mapped[str] = mapped_column(String(26), ForeignKey("files.file_id"), nullable=False)
    task_group_id: Mapped[str | None] = mapped_column(String(26), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=TaskStatus.PENDING)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    assigned_server_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("server_instances.server_id"))
    external_vendor: Mapped[str | None] = mapped_column(String(32))
    external_task_id: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16), default="zh")
    options_json: Mapped[str | None] = mapped_column(Text)
    result_path: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    callback_url: Mapped[str | None] = mapped_column(Text)
    callback_secret: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    file: Mapped["File"] = relationship(back_populates="tasks", lazy="selectin")  # type: ignore[name-defined]
    events: Mapped[list["TaskEvent"]] = relationship(back_populates="task", lazy="selectin")  # type: ignore[name-defined]

    @property
    def file_name(self) -> str | None:
        """Original filename from the related File record.

        Returns None when the relationship hasn't been loaded
        (avoids MissingGreenlet in async contexts).
        """
        try:
            return self.file.original_name if self.file else None
        except Exception:
            return None

    def can_transition_to(self, new_status: TaskStatus) -> bool:
        current = TaskStatus(self.status)
        return new_status in VALID_TRANSITIONS.get(current, set())

    def transition_to(self, new_status: TaskStatus) -> str:
        current = TaskStatus(self.status)
        if not self.can_transition_to(new_status):
            raise ValueError(f"Invalid transition: {current} → {new_status}")
        from_status = self.status
        self.status = new_status.value
        lo, _ = STATUS_PROGRESS_RANGES[new_status]
        if new_status not in (TaskStatus.FAILED, TaskStatus.CANCELED):
            self.progress = lo
        if new_status == TaskStatus.SUCCEEDED:
            self.progress = 1.0
            self.completed_at = datetime.now(timezone.utc)
        if new_status == TaskStatus.FAILED:
            self.completed_at = datetime.now(timezone.utc)
        return from_status

    def __repr__(self) -> str:
        return f"<Task {self.task_id} [{self.status}] progress={self.progress:.1%}>"
