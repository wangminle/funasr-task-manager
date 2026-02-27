"""Unit tests for CLI commands using Typer CliRunner."""

import json
import pytest
from unittest.mock import MagicMock, patch
from typer.testing import CliRunner

from cli.main import app
from cli.api_client import ASRClient, APIError

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_api_client():
    """Patch ASRClient for all CLI command tests."""
    mock = MagicMock(spec=ASRClient)
    with patch("cli.main.ASRClient", return_value=mock) as factory:
        factory._instance = mock
        yield mock


def _get_mock(monkeypatch=None):
    """Retrieve the mocked ASRClient from the latest patch."""
    from unittest.mock import patch as p
    import cli.main
    return cli.main.ASRClient.return_value if hasattr(cli.main.ASRClient, 'return_value') else MagicMock()


class TestVersion:
    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.stdout

    def test_help(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "ASR Task Manager CLI" in result.stdout


class TestHealthCommand:
    def test_health_ok(self, _mock_api_client):
        _mock_api_client.health.return_value = {"status": "ok", "version": "0.1.0"}
        result = runner.invoke(app, ["--output", "json", "health"])
        assert result.exit_code == 0

    def test_health_failure(self, _mock_api_client):
        _mock_api_client.health.side_effect = APIError(500, "down")
        result = runner.invoke(app, ["health"])
        assert result.exit_code == 1


class TestStatsCommand:
    def test_stats_ok(self, _mock_api_client):
        _mock_api_client.stats.return_value = {
            "server_total": 2, "server_online": 1, "slots_total": 8,
            "slots_used": 3, "queue_depth": 5, "tasks_today_completed": 10,
            "tasks_today_failed": 1, "success_rate_24h": 95.5, "avg_rtf": 0.15,
        }
        result = runner.invoke(app, ["--output", "json", "stats"])
        assert result.exit_code == 0


class TestConfigCommand:
    def test_config_set_get(self, tmp_path):
        import cli.config_store as cs
        cs.CONFIG_PATH = tmp_path / ".test-cli.yaml"
        result = runner.invoke(app, ["config", "set", "server", "http://new:9090"])
        assert result.exit_code == 0
        result = runner.invoke(app, ["config", "get", "server"])
        assert result.exit_code == 0
        assert "http://new:9090" in result.stdout

    def test_config_list(self, tmp_path):
        import cli.config_store as cs
        cs.CONFIG_PATH = tmp_path / ".test-cli2.yaml"
        result = runner.invoke(app, ["config", "list"])
        assert result.exit_code == 0


class TestTaskCommands:
    def test_task_list_json(self, _mock_api_client):
        _mock_api_client.list_tasks.return_value = {
            "items": [{"task_id": "T1", "status": "SUCCEEDED", "progress": 1.0,
                        "language": "zh", "created_at": "2026-02-27T10:00:00"}],
            "total": 1, "page": 1, "page_size": 20,
        }
        result = runner.invoke(app, ["--output", "json", "task", "list"])
        assert result.exit_code == 0

    def test_task_info(self, _mock_api_client):
        _mock_api_client.get_task.return_value = {
            "task_id": "T1", "status": "SUCCEEDED", "progress": 1.0,
            "eta_seconds": None, "language": "zh", "file_id": "F1",
            "assigned_server_id": "s1", "retry_count": 0,
            "error_message": None, "created_at": "2026-02-27T10:00:00",
            "completed_at": "2026-02-27T10:05:00",
        }
        result = runner.invoke(app, ["--output", "json", "task", "info", "T1"])
        assert result.exit_code == 0

    def test_task_cancel(self, _mock_api_client):
        _mock_api_client.cancel_task.return_value = {"task_id": "T1", "status": "CANCELED"}
        result = runner.invoke(app, ["task", "cancel", "T1"])
        assert result.exit_code == 0

    def test_task_result_to_stdout(self, _mock_api_client):
        _mock_api_client.get_result.return_value = '{"text": "hello world"}'
        result = runner.invoke(app, ["task", "result", "T1", "--format", "json"])
        assert result.exit_code == 0
        assert "hello world" in result.stdout

    def test_task_result_to_file(self, _mock_api_client, tmp_path):
        _mock_api_client.get_result.return_value = "hello world"
        out_file = tmp_path / "out.txt"
        result = runner.invoke(app, ["task", "result", "T1", "--format", "txt", "--save", str(out_file)])
        assert result.exit_code == 0
        assert out_file.read_text() == "hello world"


class TestTranscribeCommand:
    def test_transcribe_no_wait_exits_zero(self, _mock_api_client, tmp_path):
        """Bug fix: --no-wait should exit 0 when submission succeeds."""
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"fake audio")
        _mock_api_client.upload_file.return_value = {"file_id": "F1", "original_name": "test.wav"}
        _mock_api_client.create_tasks.return_value = [{"task_id": "T1", "status": "PREPROCESSING"}]
        result = runner.invoke(app, [
            "--output", "json", "transcribe", str(test_file), "--no-wait",
        ])
        assert result.exit_code == 0, f"--no-wait should exit 0, got: {result.stdout}"

    def test_transcribe_all_upload_fail_exits_nonzero(self, _mock_api_client, tmp_path):
        """Bug fix: all files failing should exit 1, not 0."""
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"fake audio")
        _mock_api_client.upload_file.side_effect = APIError(500, "server error")
        result = runner.invoke(app, ["transcribe", str(test_file)])
        assert result.exit_code == 1

    def test_transcribe_all_create_fail_exits_nonzero(self, _mock_api_client, tmp_path):
        """Bug fix: task creation failure for all files should exit 1."""
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"fake audio")
        _mock_api_client.upload_file.return_value = {"file_id": "F1"}
        _mock_api_client.create_tasks.side_effect = APIError(500, "create failed")
        result = runner.invoke(app, ["transcribe", str(test_file)])
        assert result.exit_code == 1

    def test_transcribe_nonexistent_file_exits_nonzero(self, _mock_api_client, tmp_path):
        result = runner.invoke(app, ["transcribe", str(tmp_path / "nope.wav")])
        assert result.exit_code == 1


class TestUploadCommand:
    def test_upload_create_task_failure_exits_nonzero(self, _mock_api_client, tmp_path):
        """Bug fix: --create-task failure should exit 1."""
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"fake audio")
        _mock_api_client.upload_file.return_value = {
            "file_id": "F1", "original_name": "test.wav", "size_bytes": 1024, "status": "READY",
        }
        _mock_api_client.create_tasks.side_effect = APIError(500, "create failed")
        result = runner.invoke(app, ["upload", str(test_file), "--create-task"])
        assert result.exit_code == 1

    def test_upload_create_task_success_table(self, _mock_api_client, tmp_path):
        """Bug fix: --create-task table should show task fields correctly."""
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"fake audio")
        _mock_api_client.upload_file.return_value = {
            "file_id": "F1", "original_name": "test.wav", "size_bytes": 1024, "status": "READY",
        }
        _mock_api_client.create_tasks.return_value = [
            {"task_id": "T1", "status": "PREPROCESSING", "file_id": "F1"},
        ]
        result = runner.invoke(app, ["--output", "json", "upload", str(test_file), "--create-task"])
        assert result.exit_code == 0

    def test_upload_simple_success(self, _mock_api_client, tmp_path):
        test_file = tmp_path / "a.wav"
        test_file.write_bytes(b"data")
        _mock_api_client.upload_file.return_value = {
            "file_id": "F1", "original_name": "a.wav", "size_bytes": 4, "status": "READY",
        }
        result = runner.invoke(app, ["upload", str(test_file)])
        assert result.exit_code == 0


class TestServerCommands:
    def test_server_list(self, _mock_api_client):
        _mock_api_client.list_servers.return_value = [
            {"server_id": "s1", "name": "node1", "host": "10.0.0.1", "port": 10095,
             "protocol_version": "v2_new", "status": "ONLINE", "max_concurrency": 4, "rtf_baseline": 0.15},
        ]
        result = runner.invoke(app, ["--output", "json", "server", "list"])
        assert result.exit_code == 0

    def test_server_register(self, _mock_api_client):
        _mock_api_client.register_server.return_value = {"server_id": "s2", "status": "ONLINE"}
        result = runner.invoke(app, ["server", "register", "--id", "s2", "--host", "10.0.0.2", "--port", "10095"])
        assert result.exit_code == 0

    def test_server_delete(self, _mock_api_client):
        _mock_api_client.delete_server.return_value = None
        result = runner.invoke(app, ["server", "delete", "s1"])
        assert result.exit_code == 0
