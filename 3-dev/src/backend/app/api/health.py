"""Health check and Prometheus metrics endpoints."""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest

from app.config import settings
import app.observability.metrics as _  # noqa: F401

router = APIRouter()


@router.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "version": settings.app_version, "service": settings.app_name}


@router.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics() -> str:
    return generate_latest().decode("utf-8")
