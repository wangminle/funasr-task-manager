"""File model - stores uploaded file metadata."""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, SmallInteger, String, Text, Float, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class File(Base, TimestampMixin):
    __tablename__ = "files"

    file_id: Mapped[str] = mapped_column(String(26), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str | None] = mapped_column(String(32))
    mime: Mapped[str | None] = mapped_column(String(128))
    duration_sec: Mapped[float | None] = mapped_column(Float)
    codec: Mapped[str | None] = mapped_column(String(64))
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(SmallInteger)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UPLOADED")

    tasks: Mapped[list["Task"]] = relationship(back_populates="file", lazy="selectin")  # type: ignore[name-defined]

    def __repr__(self) -> str:
        return f"<File {self.file_id} '{self.original_name}' ({self.status})>"
