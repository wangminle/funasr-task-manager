"""TaskSegment model — internal work unit for VAD-based parallel transcription."""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import DateTime, Float, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SegmentStatus(StrEnum):
    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    TRANSCRIBING = "TRANSCRIBING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class TaskSegment(Base):
    __tablename__ = "task_segments"

    segment_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    segment_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    source_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    keep_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    keep_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SegmentStatus.PENDING,
    )
    assigned_server_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("server_instances.server_id"), nullable=True,
    )
    retry_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    raw_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped["Task"] = relationship(back_populates="segments", lazy="selectin")  # type: ignore[name-defined]

    @property
    def duration_ms(self) -> int:
        return self.source_end_ms - self.source_start_ms

    @property
    def keep_duration_ms(self) -> int:
        return self.keep_end_ms - self.keep_start_ms

    def __repr__(self) -> str:
        return (
            f"<TaskSegment {self.segment_id} "
            f"task={self.task_id} idx={self.segment_index} [{self.status}]>"
        )
