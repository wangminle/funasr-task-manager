"""System diagnostics: schema validation, dependency checks, server connectivity."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DiagnosticCheck:
    name: str
    level: str  # "ok" | "warning" | "error"
    detail: str


@dataclass
class DiagnosticReport:
    checks: list[DiagnosticCheck] = field(default_factory=list)

    @property
    def has_blocking_errors(self) -> bool:
        return any(c.level == "error" for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "checks": [{"name": c.name, "level": c.level, "detail": c.detail} for c in self.checks],
            "has_blocking_errors": self.has_blocking_errors,
        }


EXPECTED_CALLBACK_OUTBOX_COLUMNS = {
    "outbox_id", "task_id", "event_id", "callback_url",
    "payload_json", "status", "retry_count", "last_error",
    "created_at", "sent_at",
}

LEGACY_CALLBACK_OUTBOX_COLUMNS = {"id", "signature", "updated_at"}


async def check_schema(session: AsyncSession) -> DiagnosticCheck:
    """Verify callback_outbox table matches the current ORM model."""
    try:
        def _inspect_sync(connection):
            insp = inspect(connection)
            if not insp.has_table("callback_outbox"):
                return None, set()
            columns = {col["name"] for col in insp.get_columns("callback_outbox")}
            return True, columns

        conn = await session.connection()
        has_table, columns = await conn.run_sync(_inspect_sync)

        if has_table is None:
            return DiagnosticCheck(
                name="database_schema",
                level="error",
                detail="callback_outbox table missing; run alembic upgrade head",
            )

        legacy_found = columns & LEGACY_CALLBACK_OUTBOX_COLUMNS
        expected_missing = EXPECTED_CALLBACK_OUTBOX_COLUMNS - columns

        if legacy_found or expected_missing:
            detail_parts = []
            if legacy_found:
                detail_parts.append(f"legacy columns present: {legacy_found}")
            if expected_missing:
                detail_parts.append(f"expected columns missing: {expected_missing}")
            return DiagnosticCheck(
                name="database_schema",
                level="error",
                detail=f"callback_outbox schema drift — {'; '.join(detail_parts)}; run alembic upgrade head",
            )

        return DiagnosticCheck(name="database_schema", level="ok", detail="schema aligned")
    except Exception as e:
        return DiagnosticCheck(name="database_schema", level="error", detail=str(e))


async def check_alembic_version(session: AsyncSession) -> DiagnosticCheck:
    """Check the current Alembic migration version."""
    try:
        result = await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        row = result.first()
        if row is None:
            return DiagnosticCheck(name="alembic_version", level="warning", detail="no alembic version found")
        return DiagnosticCheck(name="alembic_version", level="ok", detail=f"version: {row[0]}")
    except Exception:
        return DiagnosticCheck(name="alembic_version", level="warning", detail="alembic_version table not found")


def check_ffprobe() -> DiagnosticCheck:
    """Check if ffprobe is available on PATH."""
    path = shutil.which("ffprobe")
    if path:
        return DiagnosticCheck(name="ffprobe", level="ok", detail=f"found at {path}")
    return DiagnosticCheck(
        name="ffprobe",
        level="warning",
        detail="ffprobe not found; audio duration will use file-size heuristic (rough estimate, may be inaccurate for video files)",
    )


def check_upload_dir() -> DiagnosticCheck:
    """Check if the upload directory exists and is writable."""
    upload_dir = Path(settings.upload_dir)
    if not upload_dir.exists():
        return DiagnosticCheck(name="upload_dir", level="error", detail=f"{upload_dir} does not exist")
    try:
        test_file = upload_dir / ".diag_write_test"
        test_file.write_text("test")
        test_file.unlink()
        return DiagnosticCheck(name="upload_dir", level="ok", detail=f"{upload_dir} writable")
    except OSError as e:
        return DiagnosticCheck(name="upload_dir", level="error", detail=f"{upload_dir} not writable: {e}")


async def check_server_connectivity(session: AsyncSession) -> DiagnosticCheck:
    """Check how many ASR servers are ONLINE."""
    try:
        result = await session.execute(text("SELECT status, COUNT(*) FROM server_instances GROUP BY status"))
        status_counts = {row[0]: row[1] for row in result.fetchall()}
        online = status_counts.get("ONLINE", 0)
        total = sum(status_counts.values())
        if total == 0:
            return DiagnosticCheck(name="asr_servers", level="warning", detail="no servers registered")
        if online == 0:
            return DiagnosticCheck(name="asr_servers", level="error", detail=f"0/{total} servers online")
        if online < total:
            return DiagnosticCheck(name="asr_servers", level="warning", detail=f"{online}/{total} online")
        return DiagnosticCheck(name="asr_servers", level="ok", detail=f"{online}/{total} online")
    except Exception as e:
        return DiagnosticCheck(name="asr_servers", level="warning", detail=f"query failed: {e}")


async def run_full_diagnostics(session: AsyncSession) -> DiagnosticReport:
    """Run all diagnostic checks and return a consolidated report."""
    report = DiagnosticReport()
    report.checks.append(await check_schema(session))
    report.checks.append(await check_alembic_version(session))
    report.checks.append(check_ffprobe())
    report.checks.append(check_upload_dir())
    report.checks.append(await check_server_connectivity(session))
    return report
