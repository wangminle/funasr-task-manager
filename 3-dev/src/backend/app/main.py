"""FastAPI application factory and lifespan management."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.observability.logging import setup_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(level=settings.log_level, fmt=settings.log_format)
    logger.info("application_starting", version=settings.app_version)

    from app.auth.token import init_auth_from_settings
    init_auth_from_settings()

    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.result_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)

    yield

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
