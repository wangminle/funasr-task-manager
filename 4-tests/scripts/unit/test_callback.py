"""Callback service unit tests (T-M3-10 to T-M3-14)."""

import json

import pytest

from app.services.callback import (
    build_callback_payload, create_outbox_record, generate_hmac_signature,
)
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
