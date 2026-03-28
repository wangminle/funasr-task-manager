"""Unit tests for system diagnostics service."""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.services.diagnostics import (
    DiagnosticCheck,
    check_ffprobe,
    check_schema,
    check_upload_dir,
    check_alembic_version,
    check_server_connectivity,
    run_full_diagnostics,
)


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess


@pytest.mark.unit
class TestCheckSchema:
    async def test_schema_ok_when_tables_created_from_orm(self, session):
        """ORM-created tables should pass schema check."""
        result = await check_schema(session)
        assert result.level == "ok"
        assert "aligned" in result.detail

    async def test_schema_error_when_table_missing(self, engine):
        """Missing callback_outbox table should be an error."""
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS callback_outbox"))

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            result = await check_schema(sess)
            assert result.level == "error"
            assert "missing" in result.detail

    async def test_schema_error_when_legacy_columns_present(self, engine):
        """Legacy columns (id, signature, updated_at) should trigger schema drift error."""
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS callback_outbox"))
            await conn.execute(text("""
                CREATE TABLE callback_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id VARCHAR(26) NOT NULL,
                    event_id VARCHAR(26) NOT NULL,
                    callback_url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    signature VARCHAR(128),
                    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
                    retry_count SMALLINT DEFAULT 0,
                    last_error TEXT,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            result = await check_schema(sess)
            assert result.level == "error"
            assert "drift" in result.detail


@pytest.mark.unit
class TestCheckFfprobe:
    def test_ffprobe_check_returns_ok_or_warning(self):
        result = check_ffprobe()
        assert result.level in ("ok", "warning")
        assert result.name == "ffprobe"


@pytest.mark.unit
class TestCheckUploadDir:
    def test_upload_dir_check(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.services.diagnostics.settings.upload_dir", tmp_path)
        result = check_upload_dir()
        assert result.level == "ok"

    def test_upload_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.services.diagnostics.settings.upload_dir", tmp_path / "nonexistent")
        result = check_upload_dir()
        assert result.level == "error"


@pytest.mark.unit
class TestCheckAlembicVersion:
    async def test_alembic_version_missing(self, session):
        result = await check_alembic_version(session)
        assert result.level == "warning"

    async def test_alembic_version_present(self, engine):
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
            await conn.execute(text("INSERT INTO alembic_version VALUES ('002_fix_outbox')"))

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            result = await check_alembic_version(sess)
            assert result.level == "ok"
            assert "002_fix_outbox" in result.detail


@pytest.mark.unit
class TestCheckServerConnectivity:
    async def test_no_servers(self, session):
        result = await check_server_connectivity(session)
        assert result.level == "warning"
        assert "no servers" in result.detail

    async def test_servers_online(self, engine):
        from app.models import ServerInstance
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            sess.add(ServerInstance(
                server_id="s1", name="S1", host="localhost", port=10095,
                protocol_version="v2_new", max_concurrency=4, status="ONLINE",
            ))
            await sess.commit()

        async with factory() as sess:
            result = await check_server_connectivity(sess)
            assert result.level == "ok"
            assert "1/1" in result.detail


@pytest.mark.unit
class TestRunFullDiagnostics:
    async def test_full_run(self, session, tmp_path, monkeypatch):
        monkeypatch.setattr("app.services.diagnostics.settings.upload_dir", tmp_path)
        report = await run_full_diagnostics(session)
        assert len(report.checks) == 5
        assert isinstance(report.has_blocking_errors, bool)
        d = report.to_dict()
        assert "checks" in d
        assert "has_blocking_errors" in d
