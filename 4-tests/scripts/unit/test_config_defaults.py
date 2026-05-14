"""Tests for backend configuration defaults."""

import pytest


@pytest.mark.unit
def test_websocket_read_idle_timeout_default_is_300_seconds():
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.websocket_read_idle_timeout_seconds == 300
