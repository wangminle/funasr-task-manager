"""FastAPI application factory and lifespan management."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.observability.logging import setup_logging, get_logger
from app.services.task_runner import task_runner
from app.services.heartbeat import heartbeat_service
from app.storage.database import async_session_factory
from app.storage.repository import ServerRepository

logger = get_logger(__name__)

_schema_ok: bool = False


def get_schema_ok() -> bool:
    return _schema_ok


async def _get_servers_for_heartbeat() -> list[dict]:
    async with async_session_factory() as session:
        repo = ServerRepository(session)
        return await repo.get_all_servers_brief()


async def _update_server_status(
    server_id: str, new_status: str, last_heartbeat: datetime | None,
) -> None:
    async with async_session_factory() as session:
        repo = ServerRepository(session)
        await repo.update_server_status(server_id, new_status, last_heartbeat)
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info("application_starting", version=settings.app_version)

    from app.auth.token import init_auth_from_settings
    init_auth_from_settings()

    if settings.rate_limit_enabled:
        from app.auth.rate_limiter import rate_limiter, RateLimitConfig
        rate_limiter.config = RateLimitConfig(
            max_concurrent_tasks=settings.rate_limit_max_concurrent,
            max_upload_bytes_per_minute=settings.rate_limit_max_upload_mb_per_min * 1024 * 1024,
            max_tasks_per_day=settings.rate_limit_max_daily,
        )
        rate_limiter.enable()
        logger.info("rate_limiter_enabled",
                     max_concurrent=settings.rate_limit_max_concurrent,
                     max_daily=settings.rate_limit_max_daily)

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.result_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)

    global _schema_ok
    try:
        from app.services.diagnostics import check_schema
        async with async_session_factory() as session:
            result = await check_schema(session)
            if result.level == "error":
                logger.critical("schema_check_failed", detail=result.detail)
                raise SystemExit(f"Schema check failed: {result.detail}")
            _schema_ok = result.level == "ok"
            if not _schema_ok:
                logger.warning("schema_check_warning", detail=result.detail)
            else:
                logger.info("schema_check_passed")
    except SystemExit:
        raise
    except Exception as e:
        logger.warning("schema_check_skipped", error=str(e))
        _schema_ok = False

    await heartbeat_service.start(_get_servers_for_heartbeat, _update_server_status)
    await task_runner.start()

    yield

    await task_runner.stop()
    await heartbeat_service.stop()
    logger.info("application_shutting_down")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    is_wildcard = origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins else ["*"],
        allow_credentials=bool(origins) and not is_wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.api.health import router as health_router
    from app.api.files import router as files_router
    from app.api.tasks import router as tasks_router
    from app.api.task_groups import router as task_groups_router
    from app.api.servers import router as servers_router
    from app.api.sse import router as sse_router
    from app.api.stats import router as stats_router
    from app.api.alerts import router as alerts_router

    app.include_router(health_router)
    app.include_router(files_router)
    app.include_router(tasks_router)
    app.include_router(task_groups_router)
    app.include_router(servers_router)
    app.include_router(sse_router)
    app.include_router(stats_router)
    app.include_router(alerts_router)

    return app


app = create_app()
