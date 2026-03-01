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

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.result_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from app.api.health import router as health_router
    from app.api.files import router as files_router
    from app.api.tasks import router as tasks_router
    from app.api.servers import router as servers_router
    from app.api.sse import router as sse_router
    from app.api.stats import router as stats_router

    app.include_router(health_router)
    app.include_router(files_router)
    app.include_router(tasks_router)
    app.include_router(servers_router)
    app.include_router(sse_router)
    app.include_router(stats_router)

    return app


app = create_app()
