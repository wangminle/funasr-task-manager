"""Unit tests for CLI config_store module."""

import pytest
from unittest.mock import patch, mock_open
from pathlib import Path


@pytest.fixture(autouse=True)
def _patch_config_path(tmp_path):
    """Redirect config file to temp dir for every test."""
    import cli.config_store as cs
    original = cs.CONFIG_PATH
    cs.CONFIG_PATH = tmp_path / ".asr-cli.yaml"
    yield
    cs.CONFIG_PATH = original


class TestConfigStore:
    def test_defaults(self):
        from cli.config_store import get_all
        cfg = get_all()
        assert cfg["server"] == "http://localhost:15797"
        assert cfg["output"] == "table"
        assert cfg["api_key"] == ""

    def test_set_and_get(self):
        from cli.config_store import get, set_value
        set_value("server", "http://custom:9090")
        assert get("server") == "http://custom:9090"

    def test_get_nonexistent_key(self):
        from cli.config_store import get
        assert get("nonexistent") is None

    def test_persistence(self, tmp_path):
        from cli.config_store import set_value, get, CONFIG_PATH
        set_value("api_key", "my-secret-key")
        assert CONFIG_PATH.exists()
        assert get("api_key") == "my-secret-key"

    def test_overwrite(self):
        from cli.config_store import set_value, get
        set_value("output", "json")
        assert get("output") == "json"
        set_value("output", "text")
        assert get("output") == "text"
