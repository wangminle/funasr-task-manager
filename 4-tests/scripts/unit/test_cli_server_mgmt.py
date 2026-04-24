"""Unit tests for CLI server management commands (P1-1).

Tests probe, benchmark, and update commands with mocked API client.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_archive():
    """Prevent benchmark archive side-effects during tests."""
    with patch("cli.commands.server._archive_benchmark"), \
         patch("cli.commands.server._last_benchmark_age_minutes", return_value=None):
        yield


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
        events = [
            *_make_benchmark_events("asr-01"),
            {"type": "benchmark_result", "server_id": "asr-01",
             "data": {"server_id": "asr-01", "reachable": True, "responsive": True,
                      "single_rtf": 0.124, "throughput_rtf": 0.031, "benchmark_concurrency": 4,
                      "recommended_concurrency": 4,
                      "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                      "benchmark_samples": ["test.mp4", "tv-report-1.wav"],
                      "benchmark_notes": [], "concurrency_gradient": []}},
        ]
        mock_client.benchmark_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "asr-01",
        ])
        assert result.exit_code == 0
        mock_client.benchmark_server_stream.assert_called_once_with("asr-01")

    def test_benchmark_success(self, mock_client):
        events = [
            {"type": "all_benchmark_start", "server_ids": ["asr-01", "asr-02"], "total_servers": 2},
            {"type": "server_benchmark_done", "server_id": "asr-01", "completed": 1, "total": 2,
             "data": {"server_id": "asr-01", "reachable": True, "responsive": True,
                      "single_rtf": 0.124, "throughput_rtf": 0.031, "benchmark_concurrency": 4,
                      "recommended_concurrency": 4, "benchmark_audio_sec": 300.0,
                      "benchmark_elapsed_sec": 37.2, "benchmark_samples": ["test.mp4", "tv-report-1.wav"],
                      "benchmark_notes": [], "concurrency_gradient": []}},
            {"type": "server_benchmark_done", "server_id": "asr-02", "completed": 2, "total": 2,
             "data": {"server_id": "asr-02", "reachable": True, "responsive": True,
                      "single_rtf": 0.737, "throughput_rtf": 0.184, "benchmark_concurrency": 4,
                      "recommended_concurrency": 4, "benchmark_audio_sec": 300.0,
                      "benchmark_elapsed_sec": 221.1, "benchmark_samples": ["test.mp4", "tv-report-1.wav"],
                      "benchmark_notes": [], "concurrency_gradient": []}},
            {"type": "all_complete", "data": {
                "results": [
                    {"server_id": "asr-01", "single_rtf": 0.124, "throughput_rtf": 0.031,
                     "benchmark_concurrency": 4, "recommended_concurrency": 4,
                     "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                     "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                     "concurrency_gradient": [], "reachable": True, "responsive": True},
                    {"server_id": "asr-02", "single_rtf": 0.737, "throughput_rtf": 0.184,
                     "benchmark_concurrency": 4, "recommended_concurrency": 4,
                     "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 221.1,
                     "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                     "concurrency_gradient": [], "reachable": True, "responsive": True},
                ],
                "capacity_comparison": [
                    {"server_id": "asr-01", "rtf": 0.124, "relative_speed": 1.0, "acceleration_ratio": 32.26},
                    {"server_id": "asr-02", "rtf": 0.737, "relative_speed": 0.168, "acceleration_ratio": 5.43},
                ],
            }},
        ]
        mock_client.benchmark_servers_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 0
        mock_client.benchmark_servers_stream.assert_called_once()

    def test_benchmark_no_servers(self, mock_client):
        from cli.api_client import APIError
        mock_client.benchmark_servers_stream.side_effect = APIError(422, "No online servers to benchmark")
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 1

    def test_benchmark_json_output(self, mock_client):
        events = [
            {"type": "all_benchmark_start", "server_ids": ["s1"], "total_servers": 1},
            {"type": "server_benchmark_done", "server_id": "s1", "completed": 1, "total": 1,
             "data": {"server_id": "s1", "single_rtf": 0.5, "throughput_rtf": 0.125,
                      "benchmark_concurrency": 4, "recommended_concurrency": 4,
                      "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 150.0,
                      "benchmark_samples": ["test.mp4", "tv-report-1.wav"],
                      "benchmark_notes": [], "concurrency_gradient": [],
                      "reachable": True, "responsive": True}},
            {"type": "all_complete", "data": {
                "results": [{"server_id": "s1", "reachable": True, "responsive": True,
                             "single_rtf": 0.5, "throughput_rtf": 0.125,
                             "benchmark_concurrency": 4, "recommended_concurrency": 4,
                             "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 150.0,
                             "benchmark_samples": ["test.mp4", "tv-report-1.wav"],
                             "benchmark_notes": [], "concurrency_gradient": []}],
                "capacity_comparison": [
                    {"server_id": "s1", "rtf": 0.5, "relative_speed": 1.0, "acceleration_ratio": 8.0}],
            }},
        ]
        mock_client.benchmark_servers_stream.return_value = iter(events)
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


def _make_benchmark_events(server_id: str = "asr-01") -> list[dict]:
    """Build a complete NDJSON benchmark event sequence."""
    return [
        {"type": "benchmark_start", "server_id": server_id, "total_phases": 2, "samples": ["test.mp4"]},
        {"type": "phase_start", "server_id": server_id, "phase": 1, "description": "单线程测速"},
        {"type": "phase_progress", "server_id": server_id, "phase": 1, "rep": 1, "total_reps": 2, "rtf": 0.15},
        {"type": "phase_progress", "server_id": server_id, "phase": 1, "rep": 2, "total_reps": 2, "rtf": 0.14},
        {"type": "phase_complete", "server_id": server_id, "phase": 1, "single_rtf": 0.14},
        {"type": "phase_start", "server_id": server_id, "phase": 2, "description": "并发梯度测试"},
        {"type": "gradient_start", "server_id": server_id, "concurrency": 1, "level_index": 1, "total_levels": 4},
        {"type": "gradient_complete", "server_id": server_id, "concurrency": 1, "throughput_rtf": 0.14, "wall_clock_sec": 5.0},
        {"type": "gradient_start", "server_id": server_id, "concurrency": 2, "level_index": 2, "total_levels": 4},
        {"type": "gradient_complete", "server_id": server_id, "concurrency": 2, "throughput_rtf": 0.08, "wall_clock_sec": 5.5},
        {"type": "benchmark_complete", "server_id": server_id, "recommended_concurrency": 2,
         "single_rtf": 0.14, "throughput_rtf": 0.08},
    ]


@pytest.mark.unit
class TestRegisterBenchmarkStream:
    """Tests for `register --benchmark` NDJSON streaming path."""

    def test_register_benchmark_stream_success(self, mock_client):
        events = [
            {"type": "server_registered", "server_id": "asr-01",
             "data": {"server_id": "asr-01", "host": "10.0.0.1", "port": 10095, "status": "ONLINE"}},
            *_make_benchmark_events("asr-01"),
            {"type": "benchmark_result", "server_id": "asr-01",
             "data": {"single_rtf": 0.14, "throughput_rtf": 0.08, "recommended_concurrency": 2}},
        ]
        mock_client.register_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000",
            "server", "register",
            "--id", "asr-01", "--host", "10.0.0.1", "--port", "10095",
            "--benchmark",
        ])
        assert result.exit_code == 0, result.output
        assert "Benchmark 完成" in result.output
        mock_client.register_server_stream.assert_called_once()

    def test_register_benchmark_stream_error(self, mock_client):
        events = [
            {"type": "server_registered", "server_id": "asr-01",
             "data": {"server_id": "asr-01", "host": "10.0.0.1", "port": 10095, "status": "ONLINE"}},
            {"type": "benchmark_error", "server_id": "asr-01", "error": "Connection refused"},
        ]
        mock_client.register_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "register",
            "--id", "asr-01", "--host", "10.0.0.1", "--port", "10095",
            "--benchmark",
        ])
        assert result.exit_code == 1
        assert "Connection refused" in result.output


@pytest.mark.unit
class TestSingleBenchmarkStream:
    """Tests for `server benchmark <id>` NDJSON streaming path."""

    def test_single_benchmark_stream_success(self, mock_client):
        events = [
            *_make_benchmark_events("asr-01"),
            {"type": "benchmark_result", "server_id": "asr-01",
             "data": {"server_id": "asr-01", "single_rtf": 0.14,
                      "throughput_rtf": 0.08, "benchmark_concurrency": 2,
                      "recommended_concurrency": 2,
                      "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                      "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                      "concurrency_gradient": [], "reachable": True, "responsive": True}},
        ]
        mock_client.benchmark_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000",
            "server", "benchmark", "asr-01",
        ])
        assert result.exit_code == 0, result.output
        mock_client.benchmark_server_stream.assert_called_once_with("asr-01")

    def test_single_benchmark_stream_error(self, mock_client):
        events = [
            {"type": "benchmark_start", "server_id": "asr-01", "total_phases": 2, "samples": ["test.mp4"]},
            {"type": "benchmark_error", "server_id": "asr-01", "error": "WebSocket timeout"},
        ]
        mock_client.benchmark_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "asr-01",
        ])
        assert result.exit_code == 1


@pytest.mark.unit
class TestBatchBenchmarkStream:
    """Tests for `server benchmark` (all servers) NDJSON streaming path."""

    def test_batch_benchmark_stream_all_fail(self, mock_client):
        events = [
            {"type": "all_benchmark_start", "server_ids": ["s1", "s2"], "total_servers": 2},
            {"type": "server_error", "server_id": "s1", "error": "timeout"},
            {"type": "server_error", "server_id": "s2", "error": "unreachable"},
        ]
        mock_client.benchmark_servers_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 1

    def test_batch_benchmark_stream_partial_fail(self, mock_client):
        events = [
            {"type": "all_benchmark_start", "server_ids": ["s1", "s2"], "total_servers": 2},
            *_make_benchmark_events("s1"),
            {"type": "server_benchmark_done", "server_id": "s1", "completed": 1, "total": 2,
             "data": {"server_id": "s1", "single_rtf": 0.14, "throughput_rtf": 0.08,
                      "benchmark_concurrency": 2, "recommended_concurrency": 2,
                      "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                      "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                      "concurrency_gradient": [], "reachable": True, "responsive": True}},
            {"type": "server_error", "server_id": "s2", "error": "timeout"},
            {"type": "all_complete", "data": {
                "results": [{"server_id": "s1", "single_rtf": 0.14, "throughput_rtf": 0.08,
                             "benchmark_concurrency": 2, "recommended_concurrency": 2,
                             "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                             "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                             "concurrency_gradient": [], "reachable": True, "responsive": True}],
                "capacity_comparison": [
                    {"server_id": "s1", "rtf": 0.14, "relative_speed": 1.0, "acceleration_ratio": 14.29}],
            }},
        ]
        mock_client.benchmark_servers_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000",
            "server", "benchmark",
        ])
        assert result.exit_code == 0, result.output
        assert "1 个失败" in result.output


@pytest.mark.unit
class TestBenchmarkSafetyChecks:
    """Tests for benchmark pre-flight safety checks."""

    def test_busy_system_blocks_benchmark(self, mock_client):
        """slots_used > 0 should block benchmark."""
        mock_client.stats.return_value = {"slots_used": 2, "queue_depth": 0}
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 1
        assert "系统繁忙" in result.output
        mock_client.benchmark_servers_stream.assert_not_called()
        mock_client.stats.assert_called_once_with(global_stats=True)

    def test_queued_tasks_blocks_benchmark(self, mock_client):
        """queue_depth > 0 should block benchmark."""
        mock_client.stats.return_value = {"slots_used": 0, "queue_depth": 1}
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark",
        ])
        assert result.exit_code == 1
        assert "系统繁忙" in result.output
        mock_client.stats.assert_called_once_with(global_stats=True)

    def test_force_bypasses_busy_check(self, mock_client):
        """--force should skip the busy-system check."""
        mock_client.stats.return_value = {"slots_used": 3, "queue_depth": 5}
        events = [
            {"type": "all_benchmark_start", "server_ids": ["s1"], "total_servers": 1},
            {"type": "server_benchmark_done", "server_id": "s1", "completed": 1, "total": 1,
             "data": {"server_id": "s1", "single_rtf": 0.3, "throughput_rtf": 0.1,
                      "benchmark_concurrency": 4, "recommended_concurrency": 4,
                      "benchmark_audio_sec": 100.0, "benchmark_elapsed_sec": 30.0,
                      "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                      "concurrency_gradient": [], "reachable": True, "responsive": True}},
            {"type": "all_complete", "data": {
                "results": [{"server_id": "s1", "single_rtf": 0.3, "throughput_rtf": 0.1,
                             "benchmark_concurrency": 4, "recommended_concurrency": 4,
                             "benchmark_samples": [], "benchmark_notes": [],
                             "concurrency_gradient": [], "reachable": True, "responsive": True}],
                "capacity_comparison": [],
            }},
        ]
        mock_client.benchmark_servers_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "--force",
        ])
        assert result.exit_code == 0
        mock_client.benchmark_servers_stream.assert_called_once()
        mock_client.stats.assert_not_called()

    def test_repeat_window_blocks_benchmark(self, mock_client):
        """Benchmark within repeat window should be blocked."""
        with patch("cli.commands.server._last_benchmark_age_minutes", return_value=3.5):
            result = runner.invoke(app, [
                "--server", "http://test:8000", "--quiet",
                "server", "benchmark",
            ])
        assert result.exit_code == 1
        assert "3.5 分钟" in result.output
        mock_client.benchmark_servers_stream.assert_not_called()

    def test_repeat_window_force_bypass(self, mock_client):
        """--force should skip the repeat-window check."""
        events = [
            {"type": "all_benchmark_start", "server_ids": ["s1"], "total_servers": 1},
            {"type": "server_benchmark_done", "server_id": "s1", "completed": 1, "total": 1,
             "data": {"server_id": "s1", "single_rtf": 0.3, "throughput_rtf": 0.1,
                      "benchmark_concurrency": 4, "recommended_concurrency": 4,
                      "benchmark_audio_sec": 100.0, "benchmark_elapsed_sec": 30.0,
                      "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                      "concurrency_gradient": [], "reachable": True, "responsive": True}},
            {"type": "all_complete", "data": {
                "results": [{"server_id": "s1", "single_rtf": 0.3, "throughput_rtf": 0.1,
                             "benchmark_concurrency": 4, "recommended_concurrency": 4,
                             "benchmark_samples": [], "benchmark_notes": [],
                             "concurrency_gradient": [], "reachable": True, "responsive": True}],
                "capacity_comparison": [],
            }},
        ]
        mock_client.benchmark_servers_stream.return_value = iter(events)
        with patch("cli.commands.server._last_benchmark_age_minutes", return_value=2.0):
            result = runner.invoke(app, [
                "--server", "http://test:8000", "--quiet",
                "server", "benchmark", "--force",
            ])
        assert result.exit_code == 0

    def test_offline_server_blocks_single_benchmark(self, mock_client):
        """Benchmarking an OFFLINE server should be blocked."""
        mock_client.stats.return_value = {"slots_used": 0, "queue_depth": 0}
        mock_client.list_servers.return_value = [
            {"server_id": "asr-01", "status": "OFFLINE", "host": "10.0.0.1", "port": 10095},
        ]
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "asr-01",
        ])
        assert result.exit_code == 1
        assert "OFFLINE" in result.output
        mock_client.benchmark_server_stream.assert_not_called()
        mock_client.stats.assert_called_once_with(global_stats=True)

    def test_force_bypasses_offline_check(self, mock_client):
        """--force should skip OFFLINE check."""
        mock_client.list_servers.return_value = [
            {"server_id": "asr-01", "status": "OFFLINE", "host": "10.0.0.1", "port": 10095},
        ]
        events = [
            *_make_benchmark_events("asr-01"),
            {"type": "benchmark_result", "server_id": "asr-01",
             "data": {"server_id": "asr-01", "single_rtf": 0.14, "throughput_rtf": 0.08,
                      "benchmark_concurrency": 2, "recommended_concurrency": 2,
                      "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                      "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                      "concurrency_gradient": [], "reachable": True, "responsive": True}},
        ]
        mock_client.benchmark_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "asr-01", "--force",
        ])
        assert result.exit_code == 0
        mock_client.benchmark_server_stream.assert_called_once_with("asr-01")
        mock_client.stats.assert_not_called()

    def test_idle_system_allows_benchmark(self, mock_client):
        """slots_used=0, queue_depth=0, no recent benchmark → should proceed."""
        mock_client.stats.return_value = {"slots_used": 0, "queue_depth": 0}
        events = [
            *_make_benchmark_events("asr-01"),
            {"type": "benchmark_result", "server_id": "asr-01",
             "data": {"server_id": "asr-01", "single_rtf": 0.14, "throughput_rtf": 0.08,
                      "benchmark_concurrency": 2, "recommended_concurrency": 2,
                      "benchmark_audio_sec": 300.0, "benchmark_elapsed_sec": 37.2,
                      "benchmark_samples": ["test.mp4"], "benchmark_notes": [],
                      "concurrency_gradient": [], "reachable": True, "responsive": True}},
        ]
        mock_client.benchmark_server_stream.return_value = iter(events)
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "server", "benchmark", "asr-01",
        ])
        assert result.exit_code == 0
        mock_client.stats.assert_called_once_with(global_stats=True)
