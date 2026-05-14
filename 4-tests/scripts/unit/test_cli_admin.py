"""Unit tests for CLI admin commands."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture
def mock_client():
    with patch("cli.main.ASRClient") as MockClient:
        client = MagicMock()
        MockClient.return_value = client
        yield client


@pytest.mark.unit
class TestAdminCli:
    def test_active_slots_calls_admin_endpoint(self, mock_client):
        mock_client.active_slots.return_value = {
            "total_active_slots": 1,
            "zombie_segments": 1,
            "servers": [
                {
                    "server_id": "asr-server-10097",
                    "status": "ONLINE",
                    "enabled": True,
                    "max_concurrency": 4,
                    "active_slots": 1,
                    "whole_tasks": [],
                    "segments": [{"segment_id": "seg1", "is_zombie": True}],
                }
            ],
        }

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--quiet",
            "admin", "active-slots",
        ])

        assert result.exit_code == 0
        mock_client.active_slots.assert_called_once_with()

    def test_emergency_stop_defaults_to_dry_run(self, mock_client):
        mock_client.emergency_stop.return_value = {
            "scope": "all",
            "group_id": None,
            "dry_run": True,
            "tasks_to_cancel": 2,
            "segments_to_release": 3,
            "active_slots_before": 3,
            "zombie_segments_before": 1,
            "tasks_canceled": 0,
            "segments_released": 0,
        }

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--quiet",
            "admin", "emergency-stop",
        ])

        assert result.exit_code == 0
        mock_client.emergency_stop.assert_called_once_with(
            scope="all", group_id=None, dry_run=True, confirm=False,
        )

    def test_emergency_stop_confirm_executes_mutating_request(self, mock_client):
        mock_client.emergency_stop.return_value = {
            "scope": "group",
            "group_id": "group-1",
            "dry_run": False,
            "tasks_to_cancel": 1,
            "segments_to_release": 1,
            "active_slots_before": 1,
            "zombie_segments_before": 0,
            "tasks_canceled": 1,
            "segments_released": 1,
            "active_slots_after": 0,
            "zombie_segments_after": 0,
        }

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--quiet",
            "admin", "emergency-stop", "--scope", "group", "--group-id", "group-1", "--confirm",
        ])

        assert result.exit_code == 0
        mock_client.emergency_stop.assert_called_once_with(
            scope="group", group_id="group-1", dry_run=False, confirm=True,
        )
