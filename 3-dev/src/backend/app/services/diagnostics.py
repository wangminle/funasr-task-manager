"""System diagnostics: schema validation, dependency checks, server connectivity."""

from __future__ import annotations

import shutil
import time
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

EXPECTED_CORE_TABLE_COLUMNS = {
    "server_instances": {
        "server_id", "status", "enabled", "max_concurrency",
    },
    "tasks": {
        "task_id", "status", "started_at", "assigned_server_id", "run_generation",
    },
    "task_segments": {
        "segment_id", "task_id", "status", "assigned_server_id", "run_generation",
    },
}


async def check_schema(session: AsyncSession) -> DiagnosticCheck:
    """Verify critical database tables match the current ORM model."""
    try:
        def _inspect_sync(connection):
            insp = inspect(connection)
            table_columns: dict[str, set[str] | None] = {}
            for table_name in ["callback_outbox", *EXPECTED_CORE_TABLE_COLUMNS.keys()]:
                if not insp.has_table(table_name):
                    table_columns[table_name] = None
                else:
                    table_columns[table_name] = {
                        col["name"] for col in insp.get_columns(table_name)
                    }
            return table_columns

        conn = await session.connection()
        table_columns = await conn.run_sync(_inspect_sync)

        callback_columns = table_columns["callback_outbox"]
        if callback_columns is None:
            return DiagnosticCheck(
                name="database_schema",
                level="error",
                detail="callback_outbox table missing; run alembic upgrade head",
            )

        legacy_found = callback_columns & LEGACY_CALLBACK_OUTBOX_COLUMNS
        expected_missing = EXPECTED_CALLBACK_OUTBOX_COLUMNS - callback_columns

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

        core_drift: list[str] = []
        for table_name, expected_columns in EXPECTED_CORE_TABLE_COLUMNS.items():
            columns = table_columns[table_name]
            if columns is None:
                core_drift.append(f"{table_name} table missing")
                continue
            missing = expected_columns - columns
            if missing:
                core_drift.append(f"{table_name} missing columns: {sorted(missing)}")
        if core_drift:
            return DiagnosticCheck(
                name="database_schema",
                level="error",
                detail=f"core schema drift — {'; '.join(core_drift)}; run alembic upgrade head",
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


_upload_dir_check_cache: tuple[float, str, DiagnosticCheck] | None = None
_UPLOAD_DIR_CACHE_TTL = 60.0  # seconds


def check_upload_dir() -> DiagnosticCheck:
    """Check if the upload directory exists and is writable (cached for 60s per path)."""
    global _upload_dir_check_cache
    upload_dir = Path(settings.upload_dir)
    cache_key = str(upload_dir.resolve())
    now = time.monotonic()
    if (
        _upload_dir_check_cache
        and _upload_dir_check_cache[1] == cache_key
        and (now - _upload_dir_check_cache[0]) < _UPLOAD_DIR_CACHE_TTL
    ):
        return _upload_dir_check_cache[2]
    if not upload_dir.exists():
        result = DiagnosticCheck(name="upload_dir", level="error", detail=f"{upload_dir} does not exist")
    else:
        try:
            test_file = upload_dir / ".diag_write_test"
            test_file.write_text("test")
            test_file.unlink()
            result = DiagnosticCheck(name="upload_dir", level="ok", detail=f"{upload_dir} writable")
        except OSError as e:
            result = DiagnosticCheck(name="upload_dir", level="error", detail=f"{upload_dir} not writable: {e}")

    _upload_dir_check_cache = (now, cache_key, result)
    return result


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
