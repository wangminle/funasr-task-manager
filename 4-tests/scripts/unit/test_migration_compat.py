"""Unit tests for callback_outbox migration compatibility.

Verifies that:
1. The ORM model works correctly with the new schema
2. Old schema can be detected as drifted
3. CallbackOutbox CRUD works after migration
"""

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.base import Base
from app.models.callback_outbox import CallbackOutbox, OutboxStatus


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


@pytest_asyncio.fixture
async def old_schema_engine():
    """Create an engine with the OLD (drifted) callback_outbox schema."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Drop the ORM-created table and recreate with old schema
        await conn.execute(text("DROP TABLE callback_outbox"))
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
    yield eng
    await eng.dispose()


@pytest.mark.unit
class TestCallbackOutboxModel:
    async def test_create_outbox_record(self, session):
        """ORM can create a CallbackOutbox record with the new schema."""
        from app.models import Task, TaskEvent, File

        file = File(
            file_id="01FILE0000000000000000000",
            user_id="test", original_name="test.wav",
            size_bytes=1000, storage_path="/tmp/test.wav", status="UPLOADED",
        )
        session.add(file)
        await session.flush()

        task = Task(
            task_id="01TASK00000000000000000000",
            user_id="test", file_id=file.file_id,
            status="SUCCEEDED", progress=1.0, language="zh",
        )
        session.add(task)
        await session.flush()

        event = TaskEvent(
            event_id="01EVENT0000000000000000000",
            task_id=task.task_id,
            from_status="TRANSCRIBING", to_status="SUCCEEDED",
        )
        session.add(event)
        await session.flush()

        outbox = CallbackOutbox(
            outbox_id="01OUTBOX000000000000000000",
            task_id=task.task_id,
            event_id=event.event_id,
            callback_url="https://example.com/callback",
            payload_json='{"test": true}',
            status=OutboxStatus.PENDING,
        )
        session.add(outbox)
        await session.flush()

        from sqlalchemy import select
        result = (await session.execute(
            select(CallbackOutbox).where(CallbackOutbox.outbox_id == outbox.outbox_id)
        )).scalar_one()

        assert result.outbox_id == "01OUTBOX000000000000000000"
        assert result.status == "PENDING"
        assert result.sent_at is None

    async def test_mark_as_sent(self, session):
        """Outbox record can be marked as SENT with sent_at timestamp."""
        from app.models import Task, TaskEvent, File

        file = File(
            file_id="02FILE0000000000000000000",
            user_id="test", original_name="test2.wav",
            size_bytes=1000, storage_path="/tmp/test2.wav", status="UPLOADED",
        )
        session.add(file)
        await session.flush()

        task = Task(
            task_id="02TASK00000000000000000000",
            user_id="test", file_id=file.file_id,
            status="SUCCEEDED", progress=1.0, language="zh",
        )
        session.add(task)
        await session.flush()

        event = TaskEvent(
            event_id="02EVENT0000000000000000000",
            task_id=task.task_id,
            from_status="TRANSCRIBING", to_status="SUCCEEDED",
        )
        session.add(event)
        await session.flush()

        outbox = CallbackOutbox(
            outbox_id="02OUTBOX000000000000000000",
            task_id=task.task_id,
            event_id=event.event_id,
            callback_url="https://example.com/callback",
            payload_json='{"test": true}',
            status=OutboxStatus.PENDING,
        )
        session.add(outbox)
        await session.flush()

        outbox.status = OutboxStatus.SENT
        outbox.sent_at = datetime.now(timezone.utc)
        await session.flush()

        from sqlalchemy import select
        result = (await session.execute(
            select(CallbackOutbox).where(CallbackOutbox.outbox_id == outbox.outbox_id)
        )).scalar_one()

        assert result.status == "SENT"
        assert result.sent_at is not None

    async def test_query_pending_callbacks(self, session):
        """Can query PENDING outbox records (the retry path)."""
        from sqlalchemy import select

        stmt = (
            select(CallbackOutbox)
            .where(
                CallbackOutbox.status == OutboxStatus.PENDING.value,
                CallbackOutbox.retry_count < 5,
            )
            .order_by(CallbackOutbox.created_at.asc())
            .limit(50)
        )
        records = list((await session.execute(stmt)).scalars().all())
        assert isinstance(records, list)


@pytest.mark.unit
class TestOldSchemaDetection:
    async def test_old_schema_detected_as_drift(self, old_schema_engine):
        """The diagnostics check should detect old schema as a drift error."""
        from app.services.diagnostics import check_schema

        factory = async_sessionmaker(old_schema_engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as sess:
            result = await check_schema(sess)
            assert result.level == "error"
            assert "drift" in result.detail
