"""Global test fixtures for ASR Task Manager."""

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

backend_root = Path(__file__).resolve().parent.parent.parent / "3-dev" / "src" / "backend"
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.models.base import Base  # noqa: E402


def pytest_configure(config):
    state_dir = Path(config.rootpath) / ".pytest-state"
    cache_dir = state_dir / "cache"
    basetemp_dir = state_dir / "tmp"
    cache_dir.mkdir(parents=True, exist_ok=True)
    basetemp_dir.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(basetemp_dir)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db_engine, db_session):
    from app.main import create_app
    from app.storage.database import get_db_session
    import app.storage.database as db_module

    app = create_app()

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db_session] = _override_db

    test_session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    original_factory = db_module.async_session_factory
    db_module.async_session_factory = test_session_factory

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    db_module.async_session_factory = original_factory


@pytest.fixture(scope="session")
def fixtures_dir():
    return Path(__file__).parent / "fixtures"
