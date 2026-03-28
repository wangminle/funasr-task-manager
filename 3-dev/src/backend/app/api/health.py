"""Health check, Prometheus metrics, and system diagnostics endpoints."""

import time
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest

from app.config import settings
from app.deps import DbSession
import app.observability.metrics as _  # noqa: F401

router = APIRouter()

_startup_time = time.time()


def _format_uptime() -> str:
    elapsed = int(time.time() - _startup_time)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}时")
    parts.append(f"{minutes}分")
    return "".join(parts)


@router.get("/health")
async def health_check() -> dict:
    db_type = "PostgreSQL" if "postgresql" in settings.database_url else "SQLite"
    return {
        "status": "ok",
        "version": settings.app_version,
        "service": settings.app_name,
        "database_type": db_type,
        "auth_enabled": settings.auth_enabled,
        "uptime": _format_uptime(),
    }


@router.get("/api/v1/diagnostics")
async def diagnostics(db: DbSession) -> dict:
    """Run system diagnostics: schema, dependencies, server connectivity."""
    from app.services.diagnostics import run_full_diagnostics
    report = await run_full_diagnostics(db)
    return report.to_dict()


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> str:
    return generate_latest().decode("utf-8")
