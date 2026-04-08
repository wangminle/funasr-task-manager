"""ServerInstance model - ASR server node registry."""

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import DateTime, Float, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ServerStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DEGRADED = "DEGRADED"


class ServerInstance(Base):
    __tablename__ = "server_instances"

    server_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    host: Mapped[str] = mapped_column(String(256), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(16), nullable=False)
    server_type: Mapped[str | None] = mapped_column(String(16))
    supported_modes: Mapped[str | None] = mapped_column(String(64))
    max_concurrency: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=4)
    rtf_baseline: Mapped[float | None] = mapped_column(Float)
    throughput_rtf: Mapped[float | None] = mapped_column(Float)
    benchmark_concurrency: Mapped[int | None] = mapped_column(SmallInteger)
    penalty_factor: Mapped[float] = mapped_column(Float, default=0.1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ServerStatus.OFFLINE)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    labels_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        server_default=func.now(), nullable=False,
    )

    def is_available(self) -> bool:
        return self.status == ServerStatus.ONLINE

    def __repr__(self) -> str:
        return f"<ServerInstance {self.server_id} {self.host}:{self.port} [{self.status}]>"
