"""SQLAlchemy async engine and session management.

Supports both SQLite (aiosqlite) and PostgreSQL (asyncpg).
Set ASR_DATABASE_URL to switch between them:
  - sqlite+aiosqlite:///./data/asr_tasks.db    (default, single instance)
  - postgresql+asyncpg://user:pw@host:5432/db   (high-concurrency, multi-instance)

Install PostgreSQL support: pip install "asr-task-manager[postgres]"
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

_engine_kwargs: dict = {"echo": settings.debug, "future": True}

if settings.database_url.startswith("postgresql"):
    _engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True})

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
