"""Unit tests for task-group short commands (scan, submit, status, download).

These commands support the async agent architecture where the main agent
dispatches tasks and sub-agents monitor progress.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


def _make_task(task_id: str, group_id: str = None, status: str = "PREPROCESSING",
               file_name: str = None):
    return {
        "task_id": task_id,
        "user_id": "test",
        "file_id": f"file-{task_id}",
        "file_name": file_name or f"{task_id}.wav",
        "task_group_id": group_id,
        "status": status,
        "progress": 1.0 if status == "SUCCEEDED" else 0.0,
        "language": "zh",
        "assigned_server_id": None,
        "result_path": None,
        "error_code": None,
        "error_message": None,
        "retry_count": 0,
        "created_at": "2026-05-06T12:00:00",
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
def audio_dir(tmp_path):
    """Create a temp directory with fake audio files."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ["meeting_01.wav", "meeting_02.mp3", "interview.m4a"]:
        (inbox / name).write_bytes(b"RIFF" + b"\x00" * 500)
    (inbox / "readme.txt").write_text("not audio")
    (inbox / "subfolder").mkdir()
    (inbox / "subfolder" / "deep.wav").write_bytes(b"RIFF" + b"\x00" * 100)
    return inbox


@pytest.mark.unit
class TestTaskGroupScan:
    def test_scan_returns_json(self, audio_dir, mock_client):
        """scan should return structured JSON with file list."""
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "scan", str(audio_dir), "--no-probe",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_files"] == 4
        assert data["source_dir"] == str(audio_dir)
        assert len(data["items"]) == 4
        assert data["total_chunks"] == 1

        names = {it["name"] for it in data["items"]}
        assert "meeting_01.wav" in names
        assert "meeting_02.mp3" in names
        assert "interview.m4a" in names
        assert "deep.wav" in names
        assert "readme.txt" not in names

    def test_scan_filters_extensions(self, audio_dir, mock_client):
        """--extensions should limit scanned file types."""
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "scan", str(audio_dir),
            "--extensions", ".wav", "--no-probe",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_files"] == 2
        assert all(it["name"].endswith(".wav") for it in data["items"])

    def test_scan_chunk_splitting(self, audio_dir, mock_client):
        """--chunk-size should split files into chunks."""
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "scan", str(audio_dir),
            "--chunk-size", "2", "--no-probe",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_chunks"] == 2
        assert data["chunks"][0]["file_count"] == 2
        assert data["chunks"][1]["file_count"] == 2

    def test_scan_nonexistent_dir(self, tmp_path, mock_client):
        """Scanning nonexistent directory should fail."""
        result = runner.invoke(app, [
            "--server", "http://test:15797",
            "task-group", "scan", str(tmp_path / "no_such_dir"),
        ])
        assert result.exit_code == 1

    def test_scan_empty_dir(self, tmp_path, mock_client):
        """Scanning empty directory should return zero files."""
        empty = tmp_path / "empty"
        empty.mkdir()
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "scan", str(empty), "--no-probe",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_files"] == 0
        assert data["items"] == []

    def test_scan_item_schema(self, audio_dir, mock_client):
        """Each scanned item should have required fields."""
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "scan", str(audio_dir), "--no-probe",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        required_fields = {"index", "path", "name", "size_bytes",
                           "duration_sec", "mtime", "fingerprint"}
        for item in data["items"]:
            assert required_fields.issubset(item.keys())
            assert isinstance(item["size_bytes"], int)
            assert item["size_bytes"] > 0


@pytest.mark.unit
class TestTaskGroupSubmit:
    def _scan_manifest(self, audio_dir, names=None):
        """Generate a minimal scan manifest for testing."""
        if names is None:
            names = ["meeting_01.wav", "meeting_02.mp3"]
        items = []
        for i, name in enumerate(names):
            p = audio_dir / name
            items.append({
                "index": i,
                "path": str(p),
                "name": name,
                "size_bytes": 504,
                "duration_sec": None,
                "mtime": "2026-05-06T12:00:00+00:00",
                "fingerprint": f"{p}:504:0",
            })
        return {
            "source_dir": str(audio_dir),
            "total_files": len(names),
            "chunk_size": 50,
            "total_chunks": 1,
            "chunks": [{
                "chunk_index": 0,
                "file_count": len(names),
                "start_index": 0,
                "end_index": len(names) - 1,
            }],
            "items": items,
        }

    def test_submit_from_file(self, audio_dir, mock_client, tmp_path):
        """submit should upload files and create tasks."""
        manifest = self._scan_manifest(audio_dir)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        group_id = "TG_TEST_001"
        mock_client.upload_file.side_effect = [
            {"file_id": "fid0"}, {"file_id": "fid1"},
        ]
        mock_client.create_tasks.return_value = [
            _make_task("tid0", group_id),
            _make_task("tid1", group_id),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 2
        assert data["total_submitted"] == 2
        assert data["upload_failures"] == 0
        assert len(data["groups"]) == 1
        assert data["groups"][0]["task_group_id"] == group_id

    def test_submit_json_output_is_clean(self, audio_dir, mock_client, tmp_path):
        """JSON output must be valid even without --quiet flag."""
        manifest = self._scan_manifest(audio_dir)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        group_id = "TG_JSON_CLEAN"
        mock_client.upload_file.side_effect = [
            {"file_id": "fid0"}, {"file_id": "fid1"},
        ]
        mock_client.create_tasks.return_value = [
            _make_task("tid0", group_id),
            _make_task("tid1", group_id),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 2

    def test_submit_single_file_returns_group_id(self, audio_dir, mock_client, tmp_path):
        """Single-file chunk must still return a usable task_group_id."""
        manifest = self._scan_manifest(audio_dir, names=["meeting_01.wav"])
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        group_id = "TG_SINGLE_001"
        mock_client.upload_file.return_value = {"file_id": "fid0"}
        mock_client.create_tasks.return_value = [
            _make_task("tid0", group_id),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 1
        assert data["groups"][0]["task_group_id"] == group_id
        assert data["groups"][0]["task_group_id"] is not None

    def test_submit_specific_chunk(self, audio_dir, mock_client, tmp_path):
        """--chunk should only submit the specified chunk."""
        items = []
        for i in range(4):
            name = f"file_{i}.wav"
            p = audio_dir / name
            p.write_bytes(b"RIFF" + b"\x00" * 100)
            items.append({
                "index": i, "path": str(p), "name": name,
                "size_bytes": 104, "duration_sec": None,
                "mtime": "2026-05-06T12:00:00+00:00",
                "fingerprint": f"{p}:104:0",
            })
        manifest = {
            "source_dir": str(audio_dir),
            "total_files": 4, "chunk_size": 2, "total_chunks": 2,
            "chunks": [
                {"chunk_index": 0, "file_count": 2, "start_index": 0, "end_index": 1},
                {"chunk_index": 1, "file_count": 2, "start_index": 2, "end_index": 3},
            ],
            "items": items,
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        mock_client.upload_file.side_effect = [
            {"file_id": "fid2"}, {"file_id": "fid3"},
        ]
        mock_client.create_tasks.return_value = [
            _make_task("tid2", "TG_002"), _make_task("tid3", "TG_002"),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
            "--chunk", "1",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 2
        assert data["groups"][0]["chunk_index"] == 1

    def test_submit_upload_failure_partial(self, audio_dir, mock_client, tmp_path):
        """Partial upload failures should still submit remaining files."""
        manifest = self._scan_manifest(audio_dir)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        from cli.api_client import APIError
        mock_client.upload_file.side_effect = [
            APIError(500, "server error"),
            {"file_id": "fid1"},
        ]
        mock_client.create_tasks.return_value = [
            _make_task("tid1", "TG_003"),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 1
        assert data["total_submitted"] == 1
        assert data["upload_failures"] == 1

    def test_submit_all_create_tasks_fail(self, audio_dir, mock_client, tmp_path):
        """Exit 1 when uploads succeed but every create_tasks call fails."""
        manifest = self._scan_manifest(audio_dir)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        from cli.api_client import APIError
        mock_client.upload_file.side_effect = [
            {"file_id": "fid0"}, {"file_id": "fid1"},
        ]
        mock_client.create_tasks.side_effect = APIError(500, "create failed")

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 2
        assert data["total_submitted"] == 0

    def test_submit_manifest_without_chunks(self, audio_dir, mock_client, tmp_path):
        """Submit should work when manifest has items but no chunks metadata."""
        manifest = self._scan_manifest(audio_dir)
        del manifest["chunks"]
        manifest["total_chunks"] = 0
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        group_id = "TG_NOCHUNK"
        mock_client.upload_file.side_effect = [
            {"file_id": "fid0"}, {"file_id": "fid1"},
        ]
        mock_client.create_tasks.return_value = [
            _make_task("tid0", group_id),
            _make_task("tid1", group_id),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 2
        assert data["groups"][0]["task_group_id"] == group_id

    def test_submit_resolves_relative_paths(self, mock_client, tmp_path):
        """Relative paths in manifest should resolve against source_dir."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "test.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        manifest = {
            "source_dir": str(inbox),
            "total_files": 1,
            "chunk_size": 50,
            "total_chunks": 0,
            "chunks": [],
            "items": [{
                "index": 0,
                "path": "test.wav",
                "name": "test.wav",
                "size_bytes": 104,
                "duration_sec": None,
                "mtime": "2026-05-06T12:00:00+00:00",
                "fingerprint": "test:104:0",
            }],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        group_id = "TG_RELPATH"
        mock_client.upload_file.return_value = {"file_id": "fid0"}
        mock_client.create_tasks.return_value = [
            _make_task("tid0", group_id),
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 1
        assert data["upload_failures"] == 0

    def test_submit_does_not_double_prefix_relative_scan_paths(self, mock_client, tmp_path):
        """Paths already containing source_dir should not become source_dir/source_dir/file."""
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "test.wav").write_bytes(b"RIFF" + b"\x00" * 100)

        manifest = {
            "source_dir": str(inbox),
            "total_files": 1,
            "chunk_size": 50,
            "total_chunks": 0,
            "chunks": [],
            "items": [{
                "index": 0,
                "path": "inbox/test.wav",
                "name": "test.wav",
                "size_bytes": 104,
                "duration_sec": None,
                "mtime": "2026-05-06T12:00:00+00:00",
                "fingerprint": "test:104:0",
            }],
        }
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        mock_client.upload_file.return_value = {"file_id": "fid0"}
        mock_client.create_tasks.return_value = [_make_task("tid0", "TG_REL_SCAN")]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "submit", "--manifest", str(manifest_path),
        ])

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total_uploaded"] == 1
        assert data["total_submitted"] == 1
        assert data["upload_failures"] == 0
        assert mock_client.upload_file.call_args.args[0] == inbox / "test.wav"


@pytest.mark.unit
class TestTaskGroupStatus:
    def test_status_running(self, mock_client):
        """status should return structured progress for a running group."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "TG_001",
            "total": 50, "succeeded": 10, "failed": 2,
            "canceled": 0, "in_progress": 38,
            "progress": 0.24, "is_complete": False,
        }
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "status", "TG_001",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["task_group_id"] == "TG_001"
        assert data["status"] == "RUNNING"
        assert data["total"] == 50
        assert data["succeeded"] == 10
        assert data["failed"] == 2
        assert data["is_complete"] is False

    def test_status_completed(self, mock_client):
        """status should report COMPLETED when all tasks are done."""
        mock_client.get_task_group.return_value = {
            "task_group_id": "TG_002",
            "total": 30, "succeeded": 28, "failed": 2,
            "canceled": 0, "in_progress": 0,
            "progress": 1.0, "is_complete": True,
        }
        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json",
            "task-group", "status", "TG_002",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["status"] == "COMPLETED"
        assert data["is_complete"] is True

    def test_status_not_found(self, mock_client):
        """status should exit 1 for nonexistent group."""
        from cli.api_client import APIError
        mock_client.get_task_group.side_effect = APIError(404, "Task group not found")
        result = runner.invoke(app, [
            "--server", "http://test:15797",
            "task-group", "status", "TG_NONEXIST",
        ])
        assert result.exit_code == 1


@pytest.mark.unit
class TestTaskGroupDownload:
    def test_download_succeeded_tasks(self, mock_client, tmp_path):
        """download should fetch results for succeeded tasks."""
        group_id = "TG_DL_001"
        mock_client.get_task_group.return_value = {
            "task_group_id": group_id,
            "total": 3, "succeeded": 2, "failed": 1,
        }
        mock_client.list_group_tasks.return_value = {
            "items": [
                _make_task("tid0", group_id, "SUCCEEDED", "audio_01.wav"),
                _make_task("tid1", group_id, "SUCCEEDED", "audio_02.mp3"),
                _make_task("tid2", group_id, "FAILED", "audio_03.wav"),
            ],
            "total": 3,
        }
        mock_client.get_result.side_effect = [
            "transcribed text 1",
            "transcribed text 2",
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "download", group_id,
            "--output-dir", str(tmp_path / "results"),
            "--format", "txt",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["downloaded"] == 2
        assert data["total_succeeded"] == 2
        assert data["download_failures"] == 0

        result_dir = tmp_path / "results"
        assert result_dir.exists()
        txt_files = list(result_dir.glob("*_result.txt"))
        assert len(txt_files) == 2

    def test_download_no_succeeded(self, mock_client, tmp_path):
        """download should exit 1 when no tasks succeeded."""
        group_id = "TG_DL_002"
        mock_client.get_task_group.return_value = {
            "task_group_id": group_id,
            "total": 2, "succeeded": 0, "failed": 2,
        }
        mock_client.list_group_tasks.return_value = {
            "items": [
                _make_task("tid0", group_id, "FAILED"),
                _make_task("tid1", group_id, "FAILED"),
            ],
            "total": 2,
        }

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "download", group_id,
            "--output-dir", str(tmp_path / "results"),
        ])
        assert result.exit_code == 1

    def test_download_multi_format(self, mock_client, tmp_path):
        """download with multiple formats should create files for each."""
        group_id = "TG_DL_003"
        mock_client.get_task_group.return_value = {
            "task_group_id": group_id,
            "total": 1, "succeeded": 1, "failed": 0,
        }
        mock_client.list_group_tasks.return_value = {
            "items": [
                _make_task("tid0", group_id, "SUCCEEDED", "audio.wav"),
            ],
            "total": 1,
        }
        mock_client.get_result.side_effect = [
            "plain text result",
            '{"text": "json result"}',
        ]

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "download", group_id,
            "--output-dir", str(tmp_path / "results"),
            "--format", "txt,json",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["downloaded"] == 1
        assert set(data["formats"]) == {"txt", "json"}

        result_dir = tmp_path / "results"
        assert any(f.suffix == ".txt" for f in result_dir.iterdir())
        assert any(f.suffix == ".json" for f in result_dir.iterdir())

    def test_download_all_results_fail(self, mock_client, tmp_path):
        """Exit 1 when succeeded tasks exist but every get_result fails."""
        group_id = "TG_DL_ALLFAIL"
        mock_client.get_task_group.return_value = {
            "task_group_id": group_id,
            "total": 2, "succeeded": 2, "failed": 0,
        }
        mock_client.list_group_tasks.return_value = {
            "items": [
                _make_task("tid0", group_id, "SUCCEEDED", "audio_01.wav"),
                _make_task("tid1", group_id, "SUCCEEDED", "audio_02.wav"),
            ],
            "total": 2,
        }
        from cli.api_client import APIError
        mock_client.get_result.side_effect = APIError(500, "download failed")

        result = runner.invoke(app, [
            "--server", "http://test:15797", "--output", "json", "--quiet",
            "task-group", "download", group_id,
            "--output-dir", str(tmp_path / "results"),
            "--format", "txt",
        ])
        assert result.exit_code == 1
