"""Unit tests for CLI batch transcribe and task group commands.

Uses mocked API client to verify command behavior without a real backend.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _make_task(task_id: str, group_id: str = None, status: str = "PREPROCESSING"):
    return {
        "task_id": task_id,
        "user_id": "test",
        "file_id": f"file-{task_id}",
        "task_group_id": group_id,
        "status": status,
        "progress": 0.0,
        "language": "zh",
        "assigned_server_id": None,
        "result_path": None,
        "error_code": None,
        "error_message": None,
        "retry_count": 0,
        "created_at": "2026-03-28T12:00:00",
        "started_at": None,
        "completed_at": None,
    }


@pytest.fixture
def mock_client():
    with patch("cli.main.ASRClient") as MockClient:
        client = MagicMock()
        MockClient.return_value = client
        yield client


@pytest.fixture
def audio_files(tmp_path):
    """Create fake audio files."""
    files = []
    for i in range(3):
        f = tmp_path / f"test_{i}.wav"
        f.write_bytes(b"RIFF" + b"\x00" * 500)
        files.append(f)
    return files


@pytest.mark.unit
class TestTranscribeBatchMode:
    def test_multi_file_triggers_batch(self, audio_files, mock_client, tmp_path):
        """Multiple files should use batch mode."""
        group_id = "GRP_0000000000000000000000"
        mock_client.upload_file.side_effect = [
            {"file_id": f"fid{i}"} for i in range(3)
        ]
        mock_client.create_tasks.return_value = [
            _make_task(f"tid{i}", group_id, "PREPROCESSING") for i in range(3)
        ]

        mock_client.list_group_tasks.return_value = {
            "items": [_make_task(f"tid{i}", group_id, "SUCCEEDED") for i in range(3)]
        }
        mock_client.get_result.return_value = "transcribed text"

        file_args = [str(f) for f in audio_files]
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "transcribe", *file_args,
            "--language", "zh", "--format", "txt",
            "--output-dir", str(tmp_path / "output"),
        ])
        assert result.exit_code == 0
        mock_client.create_tasks.assert_called_once()
        call_items = mock_client.create_tasks.call_args[0][0]
        assert len(call_items) == 3
        mock_client.list_group_tasks.assert_called()

    def test_single_file_uses_single_mode(self, audio_files, mock_client, tmp_path):
        """Single file should use single mode (backward compat)."""
        mock_client.upload_file.return_value = {"file_id": "fid0"}
        mock_client.create_tasks.return_value = [_make_task("tid0", status="PREPROCESSING")]

        def mock_get_task(task_id):
            return _make_task(task_id, status="SUCCEEDED")
        mock_client.get_task.side_effect = mock_get_task
        mock_client.get_result.return_value = "transcribed text"

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "transcribe", str(audio_files[0]),
            "--language", "zh", "--format", "txt",
            "--output-dir", str(tmp_path / "output"),
        ])
        assert result.exit_code == 0

    def test_batch_no_wait(self, audio_files, mock_client):
        """--no-wait should return immediately."""
        group_id = "GRP_0000000000000000000001"
        mock_client.upload_file.side_effect = [
            {"file_id": f"fid{i}"} for i in range(3)
        ]
        mock_client.create_tasks.return_value = [
            _make_task(f"tid{i}", group_id, "PREPROCESSING") for i in range(3)
        ]

        file_args = [str(f) for f in audio_files]
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "transcribe", *file_args,
            "--no-wait", "--json-summary",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["task_group_id"] == group_id
        assert len(data["task_ids"]) == 3

    def test_nonexistent_files(self, tmp_path):
        """All nonexistent files should fail."""
        result = runner.invoke(app, [
            "--server", "http://test:8000",
            "transcribe",
            str(tmp_path / "nonexistent.wav"),
        ])
        assert result.exit_code == 1


@pytest.mark.unit
class TestTaskGroupCLICommands:
    def test_task_list_with_group(self, mock_client):
        """task list --group should call list_group_tasks."""
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP01",
            "items": [_make_task("t1", "GRP01"), _make_task("t2", "GRP01")],
            "total": 2, "page": 1, "page_size": 100,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "list", "--group", "GRP01",
        ])
        assert result.exit_code == 0
        mock_client.list_group_tasks.assert_called_once_with("GRP01", page=1, page_size=20)

    def test_task_wait_with_group(self, mock_client):
        """task wait --group should poll group stats."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP01",
            "total": 2, "succeeded": 2, "failed": 0, "canceled": 0,
            "in_progress": 0, "progress": 1.0, "is_complete": True,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "wait", "--group", "GRP01",
        ])
        assert result.exit_code == 0

    def test_task_delete_with_group(self, mock_client):
        """task delete --group should call delete_task_group."""
        mock_client.delete_task_group.return_value = {
            "deleted": 3, "skipped_active": 0, "total": 3,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "delete", "--group", "GRP01",
        ])
        assert result.exit_code == 0
        mock_client.delete_task_group.assert_called_once_with("GRP01")


@pytest.mark.unit
class TestDoctorCLI:
    def test_doctor_no_errors(self, mock_client):
        """doctor should succeed when no blocking errors."""
        mock_client.diagnostics.return_value = {
            "checks": [
                {"name": "database_schema", "level": "ok", "detail": "schema aligned"},
                {"name": "ffprobe", "level": "warning", "detail": "not found"},
                {"name": "upload_dir", "level": "ok", "detail": "/tmp writable"},
            ],
            "has_blocking_errors": False,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "doctor",
        ])
        assert result.exit_code == 0

    def test_doctor_with_blocking_error(self, mock_client):
        """doctor should exit 1 when blocking errors exist."""
        mock_client.diagnostics.return_value = {
            "checks": [
                {"name": "database_schema", "level": "error", "detail": "schema drift"},
            ],
            "has_blocking_errors": True,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "doctor",
        ])
        assert result.exit_code == 1
