"""SQLAlchemy async engine and session management.

Supports both SQLite (aiosqlite) and PostgreSQL (asyncpg).
Set ASR_DATABASE_URL to switch between them:
    - sqlite+aiosqlite:////.../runtime/storage/asr_tasks.db    (default, single instance)
  - postgresql+asyncpg://user:pw@host:5432/db   (high-concurrency, multi-instance)

Install PostgreSQL support: pip install "asr-task-manager[postgres]"
"""

from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")
_engine_kwargs: dict = {"echo": settings.debug, "future": True}

if settings.database_url.startswith("postgresql"):
    _engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True})

if _is_sqlite:
    _engine_kwargs.update({
        "connect_args": {"timeout": 30},
        "pool_pre_ping": True,
    })

engine = create_async_engine(settings.database_url, **_engine_kwargs)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_on_connect(dbapi_conn, connection_record):
    """Enable WAL mode and tune SQLite for concurrent access.

    WAL (Write-Ahead Logging) allows concurrent reads during writes and
    is far more resilient to file-sync tools (Syncthing/极空间) that may
    replace the main DB file underneath us — WAL keeps the journal in
    separate -wal/-shm files instead of creating/deleting a single
    rollback journal on every transaction.
    """
    if not _is_sqlite:
        return
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def verify_db_writable() -> bool:
    """Quick smoke test: can we actually write to the database?

    Call at startup to catch readonly-database early (e.g. file-sync
    tools replaced the DB file, or permission issues).
    """
    try:
        async with async_session_factory() as session:
            await session.execute(text("CREATE TABLE IF NOT EXISTS _healthcheck (id INTEGER PRIMARY KEY)"))
            await session.execute(text("INSERT OR REPLACE INTO _healthcheck (id) VALUES (1)"))
            await session.commit()
        return True
    except Exception as exc:
        logger.error("db_write_check_failed", error=str(exc))
        return False
