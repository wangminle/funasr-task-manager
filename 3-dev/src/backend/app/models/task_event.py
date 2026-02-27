"""TaskEvent model - audit trail for task state transitions."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TaskEvent(Base):
    __tablename__ = "task_events"

    event_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(26), ForeignKey("tasks.task_id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )

    task: Mapped["Task"] = relationship(back_populates="events")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<TaskEvent {self.event_id} {self.from_status}→{self.to_status}>"
