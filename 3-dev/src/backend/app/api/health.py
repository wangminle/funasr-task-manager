"""Health check, Prometheus metrics, and system diagnostics endpoints."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest

from app.config import settings
from app.deps import DbSession
from app.auth.token import verify_admin
import app.observability.metrics as _  # noqa: F401

router = APIRouter()

AdminUser = Annotated[str, Depends(verify_admin)]

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
    from app.main import get_schema_ok
    db_type = "PostgreSQL" if "postgresql" in settings.database_url else "SQLite"
    return {
        "status": "ok",
        "version": settings.app_version,
        "service": settings.app_name,
        "database_type": db_type,
        "auth_enabled": settings.auth_enabled,
        "schema_ok": get_schema_ok(),
        "uptime": _format_uptime(),
    }


@router.get("/api/v1/diagnostics")
async def diagnostics(db: DbSession, admin: AdminUser) -> dict:
    """Run system diagnostics: schema, dependencies, server connectivity.

    Requires admin authentication — exposes internal schema and server info.
    """
    from app.services.diagnostics import run_full_diagnostics
    report = await run_full_diagnostics(db)
    return report.to_dict()


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> str:
    return generate_latest().decode("utf-8")
