"""FastAPI application factory and lifespan management."""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import or_, select, update

from app.config import settings
from app.models import Task, TaskStatus
from app.observability.logging import setup_logging, get_logger
from app.services.task_runner import task_runner
from app.services.heartbeat import heartbeat_service
from app.storage.database import async_session_factory, verify_db_writable
from app.storage.repository import ServerRepository

logger = get_logger(__name__)

_schema_ok: bool = False
_git_sha: str = "unknown"


def get_schema_ok() -> bool:
    return _schema_ok


def get_git_sha() -> str:
    return _git_sha


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


def _get_git_short_sha() -> str:
    """Best-effort git HEAD short SHA for startup banner."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging(level=settings.log_level, fmt=settings.log_format)
    global _git_sha
    _git_sha = _get_git_short_sha()
    logger.info(
        "application_starting",
        version=settings.app_version,
        git_sha=_git_sha,
        python_version=__import__("sys").version.split()[0],
        stale_task_timeout_minutes=settings.stale_task_timeout_minutes,
        task_timeout_seconds=settings.task_timeout_seconds,
        websocket_ping_interval=settings.websocket_ping_interval_seconds,
        websocket_read_idle_timeout=settings.websocket_read_idle_timeout_seconds,
    )

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

    if not await verify_db_writable():
        db_path = settings.database_url.split("///")[-1] if "///" in settings.database_url else "unknown"
        logger.critical(
            "database_readonly_at_startup",
            db_path=db_path,
            hint="Check file permissions and ensure no file-sync tool "
                 "(Syncthing/极空间/Dropbox) is syncing the runtime/ directory. "
                 "Add runtime/ to .stignore or the sync exclusion list.",
        )
        raise SystemExit(
            f"Database is readonly: {db_path}\n"
            "Likely cause: a file-sync tool replaced the DB file. "
            "Exclude runtime/ from sync and restart."
        )

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

    stale_minutes = settings.stale_task_timeout_minutes
    async with async_session_factory() as session:
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)

        # Only reset tasks that either never started or have been running
        # longer than stale_minutes.  Tasks with a recent started_at are
        # assumed to still be running from a previous process and will be
        # handled by the late-completion recovery in _mark_task_succeeded.
        result = await session.execute(
            update(Task)
            .where(
                Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
                or_(Task.started_at < stale_cutoff, Task.started_at.is_(None)),
            )
            .values(status=TaskStatus.QUEUED, assigned_server_id=None, started_at=None)
        )
        stale_count = result.rowcount

        # Count tasks that we intentionally do NOT reset (recently started,
        # possibly still being processed by a lingering FunASR connection).
        from sqlalchemy import func as sa_func
        recent_result = await session.execute(
            select(sa_func.count())
            .where(
                Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
                Task.started_at >= stale_cutoff,
            )
        )
        recent_count = recent_result.scalar() or 0

        if stale_count > 0 or recent_count > 0:
            logger.warning(
                "startup_task_recovery",
                reset_count=stale_count,
                preserved_count=recent_count,
                stale_cutoff_minutes=stale_minutes,
                hint=f"Reset {stale_count} stale tasks to QUEUED; "
                     f"preserved {recent_count} recently-started tasks "
                     f"(started within last {stale_minutes} min, may still be "
                     f"processing — late completions handled by recovery path)",
            )

        prep_result = await session.execute(
            update(Task)
            .where(
                Task.status == TaskStatus.PREPROCESSING,
                Task.started_at.is_not(None),
            )
            .values(started_at=None)
        )
        prep_count = prep_result.rowcount
        if prep_count > 0:
            logger.warning(
                "reset_orphaned_preprocessing_claims",
                count=prep_count,
            )

        await session.commit()

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
    if not origins:
        logger.warning(
            "cors_origins_empty",
            hint="No CORS origins configured. CORS middleware is NOT added — "
                 "cross-origin requests will be rejected. "
                 "Set ASR_CORS_ORIGINS to enable (use '*' for development only).",
        )
    else:
        if is_wildcard:
            logger.warning(
                "cors_wildcard_enabled",
                hint="CORS allows all origins (*). Restrict ASR_CORS_ORIGINS in production.",
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=not is_wildcard,
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
