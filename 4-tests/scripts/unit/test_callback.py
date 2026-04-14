"""Callback service unit tests (T-M3-10 to T-M3-14)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.callback import (
    build_callback_payload,
    create_outbox_record,
    deliver_callback,
    generate_hmac_signature,
)
from app.config import settings
from app.models.callback_outbox import OutboxStatus


@pytest.mark.unit
class TestCallbackPayload:
    def test_build_payload(self):
        payload = build_callback_payload("t1", "e1", "SUCCEEDED", progress=1.0, result_path="/results/t1")
        data = json.loads(payload)
        assert data["task_id"] == "t1"
        assert data["event_id"] == "e1"
        assert data["status"] == "SUCCEEDED"
        assert data["result_path"] == "/results/t1"
        assert "timestamp" in data

    def test_build_payload_with_error(self):
        payload = build_callback_payload("t1", "e1", "FAILED", error_message="timeout")
        data = json.loads(payload)
        assert data["error_message"] == "timeout"


@pytest.mark.unit
class TestHMACSignature:
    def test_generate_signature(self):
        sig = generate_hmac_signature('{"test": 1}', "secret123")
        assert isinstance(sig, str)
        assert len(sig) == 64

    def test_same_input_same_signature(self):
        sig1 = generate_hmac_signature("payload", "key")
        sig2 = generate_hmac_signature("payload", "key")
        assert sig1 == sig2

    def test_different_input_different_signature(self):
        sig1 = generate_hmac_signature("payload1", "key")
        sig2 = generate_hmac_signature("payload2", "key")
        assert sig1 != sig2


@pytest.mark.unit
class TestOutboxRecord:
    def test_create_record(self):
        record = create_outbox_record("t1", "e1", "https://example.com/hook", "SUCCEEDED")
        assert record.task_id == "t1"
        assert record.callback_url == "https://example.com/hook"
        assert record.status == OutboxStatus.PENDING
        assert len(record.outbox_id) == 26


@pytest.mark.unit
class TestDeliverCallback:
    async def test_ssrf_protection_disabled_by_default_skips_url_validation(self, monkeypatch):
        record = create_outbox_record("t1", "e1", "http://192.168.1.10/hook", "SUCCEEDED")
        post = AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok"))

        monkeypatch.setattr(settings, "ssrf_protection_enabled", False)
        monkeypatch.setattr("app.services.callback._get_shared_client", lambda: SimpleNamespace(post=post))
        monkeypatch.setattr(
            "app.services.callback.validate_callback_url_async",
            AsyncMock(side_effect=AssertionError("validation should be skipped when disabled")),
        )

        ok = await deliver_callback(record)

        assert ok is True
        assert record.status == OutboxStatus.SENT
        post.assert_awaited_once()

    async def test_ssrf_protection_enabled_blocks_private_callback_url(self, monkeypatch):
        record = create_outbox_record("t1", "e1", "http://192.168.1.10/hook", "SUCCEEDED")
        post = AsyncMock(return_value=SimpleNamespace(status_code=200, text="ok"))

        monkeypatch.setattr(settings, "ssrf_protection_enabled", True)
        monkeypatch.setattr("app.services.callback._get_shared_client", lambda: SimpleNamespace(post=post))
        monkeypatch.setattr(
            "app.services.callback.validate_callback_url_async",
            AsyncMock(return_value="Callback URL must not point to private/internal addresses: 192.168.1.10"),
        )

        ok = await deliver_callback(record)

        assert ok is False
        assert record.status == OutboxStatus.PENDING
        assert record.retry_count == 1
        assert "URL validation" in (record.last_error or "")
        post.assert_not_awaited()
