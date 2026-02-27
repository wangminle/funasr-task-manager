"""Structured logging tests."""

import json
import logging
from io import StringIO

import pytest
import structlog


@pytest.mark.unit
def test_structlog_json_output():
    from app.observability.logging import setup_logging, get_logger

    setup_logging(level="DEBUG", fmt="json")

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    existing_handler = logging.getLogger().handlers[0]
    handler.setFormatter(existing_handler.formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)

    structlog.reset_defaults()
    setup_logging(level="DEBUG", fmt="json")
    root.handlers.clear()
    root.addHandler(handler)

    log = get_logger("test")
    log.info("test_event", key="value")

    output = stream.getvalue().strip()
    assert output, "No log output captured"

    record = json.loads(output)
    assert "timestamp" in record
    assert record["level"] == "info"
    assert record["event"] == "test_event"
    assert record["key"] == "value"
