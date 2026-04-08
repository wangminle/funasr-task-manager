"""Unit tests for CLI server management commands (P1-1).

Tests probe, benchmark, and update commands with mocked API client.
"""

import json
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
class TestServerProbe:
    def test_probe_reachable(self, mock_client):
        mock_client.probe_server.return_value = {
            "server_id": "asr-01",
            "reachable": True,
            "responsive": True,
            "inferred_server_type": "funasr_main",
            "supports_offline": True,
            "supports_2pass": False,
            "supports_online": False,
            "benchmark_rtf": None,
            "probe_duration_ms": 125.4,
            "error": None,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "probe", "asr-01",
        ])
        assert result.exit_code == 0
        mock_client.probe_server.assert_called_once_with("asr-01", level="offline_light")

    def test_probe_unreachable(self, mock_client):
        mock_client.probe_server.return_value = {
            "server_id": "asr-02",
            "reachable": False,
            "responsive": False,
            "error": "Connection refused",
            "inferred_server_type": "unknown",
            "supports_offline": None,
            "supports_2pass": None,
            "supports_online": None,
            "benchmark_rtf": None,
            "probe_duration_ms": 5002.1,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "probe", "asr-02",
        ])
        assert result.exit_code == 1

    def test_probe_with_custom_level(self, mock_client):
        mock_client.probe_server.return_value = {
            "server_id": "asr-01",
            "reachable": True,
            "responsive": True,
            "inferred_server_type": "funasr_main",
            "probe_duration_ms": 2500.0,
            "supports_offline": True,
            "supports_2pass": True,
            "supports_online": False,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "probe", "asr-01", "--level", "twopass_full",
        ])
        assert result.exit_code == 0
        mock_client.probe_server.assert_called_once_with("asr-01", level="twopass_full")

    def test_probe_api_error(self, mock_client):
        from cli.api_client import APIError
        mock_client.probe_server.side_effect = APIError(404, "Server not found")
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "probe", "nonexistent",
        ])
        assert result.exit_code == 1


@pytest.mark.unit
class TestServerBenchmark:
    def test_single_server_benchmark_success(self, mock_client):
        mock_client.benchmark_server.return_value = {
            "server_id": "asr-01", "reachable": True, "responsive": True,
            "single_rtf": 0.124, "throughput_rtf": 0.031, "benchmark_concurrency": 4,
            "benchmark_audio_sec": 300.0,
            "benchmark_elapsed_sec": 37.2, "benchmark_samples": ["test.mp4", "tv-report-1.wav"],
            "benchmark_notes": [], "error": None, "concurrency_gradient": [],
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "asr-01",
        ])
        assert result.exit_code == 0
        mock_client.benchmark_server.assert_called_once_with("asr-01")

    def test_benchmark_success(self, mock_client):
        mock_client.benchmark_servers.return_value = {
            "results": [
                {"server_id": "asr-01", "reachable": True, "responsive": True, "single_rtf": 0.124, "throughput_rtf": 0.031, "benchmark_concurrency": 4, "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2, "benchmark_samples": ["test.mp4", "tv-report-1.wav"], "benchmark_notes": [], "concurrency_gradient": []},
                {"server_id": "asr-02", "reachable": True, "responsive": True, "single_rtf": 0.737, "throughput_rtf": 0.184, "benchmark_concurrency": 4, "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 221.1, "benchmark_samples": ["test.mp4", "tv-report-1.wav"], "benchmark_notes": [], "concurrency_gradient": []},
            ],
            "capacity_comparison": [
                {"server_id": "asr-01", "rtf": 0.124, "relative_speed": 1.0, "acceleration_ratio": 32.26},
                {"server_id": "asr-02", "rtf": 0.737, "relative_speed": 0.168, "acceleration_ratio": 5.43},
            ],
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 0
        mock_client.benchmark_servers.assert_called_once()

    def test_benchmark_no_servers(self, mock_client):
        from cli.api_client import APIError
        mock_client.benchmark_servers.side_effect = APIError(422, "No online servers to benchmark")
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 1

    def test_benchmark_json_output(self, mock_client):
        mock_client.benchmark_servers.return_value = {
            "results": [{"server_id": "s1", "reachable": True, "responsive": True, "single_rtf": 0.5, "throughput_rtf": 0.125, "benchmark_concurrency": 4, "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 150.0, "benchmark_samples": ["test.mp4", "tv-report-1.wav"], "benchmark_notes": [], "concurrency_gradient": []}],
            "capacity_comparison": [
                {"server_id": "s1", "rtf": 0.5, "relative_speed": 1.0, "acceleration_ratio": 8.0}
            ],
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--output", "json", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "results" in data
        assert "capacity_comparison" in data


@pytest.mark.unit
class TestServerUpdate:
    def test_update_max_concurrency(self, mock_client):
        mock_client.update_server.return_value = {
            "server_id": "asr-01", "name": "ASR 01", "host": "10.0.0.1",
            "port": 10095, "protocol_version": "v2_new",
            "max_concurrency": 8, "status": "ONLINE",
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "update", "asr-01", "--max-concurrency", "8",
        ])
        assert result.exit_code == 0
        mock_client.update_server.assert_called_once_with("asr-01", {"max_concurrency": 8})

    def test_update_multiple_fields(self, mock_client):
        mock_client.update_server.return_value = {
            "server_id": "asr-01", "name": "New Name", "host": "10.0.0.1",
            "port": 10095, "protocol_version": "v2_new",
            "max_concurrency": 6, "status": "ONLINE",
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "update", "asr-01", "--name", "New Name", "--max-concurrency", "6",
        ])
        assert result.exit_code == 0
        call_data = mock_client.update_server.call_args[0][1]
        assert call_data["name"] == "New Name"
        assert call_data["max_concurrency"] == 6

    def test_update_no_fields(self, mock_client):
        """Should fail if no fields are specified."""
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "update", "asr-01",
        ])
        assert result.exit_code == 1

    def test_update_nonexistent_server(self, mock_client):
        from cli.api_client import APIError
        mock_client.update_server.side_effect = APIError(404, "Server not found")
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "update", "nonexistent", "--name", "x",
        ])
        assert result.exit_code == 1
