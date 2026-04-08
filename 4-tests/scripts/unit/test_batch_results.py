"""Unit tests for batch result delivery enhancements (P1-2).

Tests multi-format export, batch-summary.json generation, and zip API.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _make_task(task_id, group_id="GRP01", status="SUCCEEDED"):
    return {
        "task_id": task_id,
        "user_id": "test",
        "file_id": f"file-{task_id}",
        "task_group_id": group_id,
        "status": status,
        "progress": 1.0 if status == "SUCCEEDED" else 0.0,
        "language": "zh",
        "error_message": "some error" if status == "FAILED" else None,
        "created_at": "2026-03-28T12:00:00",
        "started_at": "2026-03-28T12:00:01",
        "completed_at": "2026-03-28T12:01:00" if status == "SUCCEEDED" else None,
        "assigned_server_id": "asr-01",
        "result_path": None,
        "error_code": None,
        "retry_count": 0,
        "eta_seconds": None,
    }


@pytest.fixture
def mock_client():
    with patch("cli.main.ASRClient") as MockClient:
        client = MagicMock()
        MockClient.return_value = client
        yield client


@pytest.mark.unit
class TestBatchResultDownload:
    def test_single_format_download(self, mock_client, tmp_path):
        """Download results in single format with summary."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP01", "total": 2, "succeeded": 2,
            "failed": 0, "canceled": 0, "in_progress": 0,
            "progress": 1.0, "is_complete": True,
        }
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP01",
            "items": [_make_task("T001"), _make_task("T002")],
            "total": 2, "page": 1, "page_size": 100,
        }
        mock_client.get_result.return_value = "转写文本内容"

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "result", "--group", "GRP01",
            "--format", "txt",
            "--output-dir", str(tmp_path),
        ])
        assert result.exit_code == 0

        summary_path = tmp_path / "batch-summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["task_group_id"] == "GRP01"
        assert summary["succeeded"] == 2
        assert summary["formats_exported"] == ["txt"]
        assert len(summary["items"]) == 2

        txt_files = list(tmp_path.glob("*_result.txt"))
        assert len(txt_files) == 2

    def test_group_download_uses_runtime_download_dir_by_default(self, mock_client, tmp_path, monkeypatch):
        """Default group download location should resolve under runtime/storage/downloads."""
        monkeypatch.setenv("ASR_PROJECT_ROOT", str(tmp_path))
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP01", "total": 1, "succeeded": 1,
            "failed": 0, "canceled": 0, "in_progress": 0,
            "progress": 1.0, "is_complete": True,
        }
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP01",
            "items": [_make_task("T001")],
            "total": 1, "page": 1, "page_size": 100,
        }
        mock_client.get_result.return_value = "转写文本内容"

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "result", "--group", "GRP01",
            "--format", "txt",
        ])
        assert result.exit_code == 0

        download_dir = tmp_path / "runtime" / "storage" / "downloads"
        summary_path = download_dir / "batch-summary.json"
        assert summary_path.exists()
        assert json.loads(summary_path.read_text(encoding="utf-8"))["output_directory"] == str(download_dir)
        assert len(list(download_dir.glob("*_result.txt"))) == 1

    def test_multi_format_download(self, mock_client, tmp_path):
        """Download results in multiple formats."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP02", "total": 1, "succeeded": 1,
            "failed": 0, "canceled": 0, "in_progress": 0,
            "progress": 1.0, "is_complete": True,
        }
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP02",
            "items": [_make_task("T003", "GRP02")],
            "total": 1, "page": 1, "page_size": 100,
        }
        mock_client.get_result.return_value = "content"

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "result", "--group", "GRP02",
            "--format", "txt,json,srt",
            "--output-dir", str(tmp_path),
        ])
        assert result.exit_code == 0

        summary = json.loads((tmp_path / "batch-summary.json").read_text(encoding="utf-8"))
        assert set(summary["formats_exported"]) == {"txt", "json", "srt"}

        assert len(list(tmp_path.glob("*_result.txt"))) == 1
        assert len(list(tmp_path.glob("*_result.json"))) == 1
        assert len(list(tmp_path.glob("*_result.srt"))) == 1

    def test_mixed_status_summary(self, mock_client, tmp_path):
        """Summary should include failed tasks."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP03", "total": 3, "succeeded": 2,
            "failed": 1, "canceled": 0, "in_progress": 0,
            "progress": 0.67, "is_complete": True,
        }
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP03",
            "items": [
                _make_task("T004", "GRP03", "SUCCEEDED"),
                _make_task("T005", "GRP03", "SUCCEEDED"),
                _make_task("T006", "GRP03", "FAILED"),
            ],
            "total": 3, "page": 1, "page_size": 100,
        }
        mock_client.get_result.return_value = "content"

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "result", "--group", "GRP03",
            "--format", "txt",
            "--output-dir", str(tmp_path),
        ])
        assert result.exit_code == 0

        summary = json.loads((tmp_path / "batch-summary.json").read_text(encoding="utf-8"))
        assert summary["succeeded"] == 2
        assert summary["failed"] == 1
        failed_items = [i for i in summary["items"] if i["status"] == "FAILED"]
        assert len(failed_items) == 1
        assert "error" in failed_items[0]

    def test_no_succeeded_tasks(self, mock_client, tmp_path):
        """Should fail when no tasks succeeded."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP04", "total": 2, "succeeded": 0,
            "failed": 2, "canceled": 0, "in_progress": 0,
            "progress": 0.0, "is_complete": True,
        }
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP04",
            "items": [
                _make_task("T007", "GRP04", "FAILED"),
                _make_task("T008", "GRP04", "FAILED"),
            ],
            "total": 2, "page": 1, "page_size": 100,
        }
        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "result", "--group", "GRP04",
            "--format", "txt",
            "--output-dir", str(tmp_path),
        ])
        assert result.exit_code == 1


@pytest.mark.unit
class TestBatchSummaryFormat:
    def test_summary_json_structure(self, mock_client, tmp_path):
        """Verify batch-summary.json has all required fields."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "GRP05", "total": 1, "succeeded": 1,
            "failed": 0, "canceled": 0, "in_progress": 0,
            "progress": 1.0, "is_complete": True,
        }
        mock_client.list_group_tasks.return_value = {
            "task_group_id": "GRP05",
            "items": [_make_task("T009", "GRP05")],
            "total": 1, "page": 1, "page_size": 100,
        }
        mock_client.get_result.return_value = "text"

        runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "task", "result", "--group", "GRP05",
            "--format", "txt",
            "--output-dir", str(tmp_path),
        ])

        summary = json.loads((tmp_path / "batch-summary.json").read_text(encoding="utf-8"))
        assert "task_group_id" in summary
        assert "total_tasks" in summary
        assert "succeeded" in summary
        assert "failed" in summary
        assert "formats_exported" in summary
        assert "output_directory" in summary
        assert "items" in summary
        assert isinstance(summary["items"], list)

        item = summary["items"][0]
        assert "task_id" in item
        assert "file_id" in item
        assert "status" in item
        assert "outputs" in item
