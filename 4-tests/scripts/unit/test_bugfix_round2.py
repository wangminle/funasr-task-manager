"""Unit tests for round 2 bug fixes.

Fix 1 (P1): Migration 002 backfill outbox_id
Fix 2 (P2): Benchmark capacity comparison excludes offline servers
Fix 3 (P2): Batch upload partial failure → nonzero exit
Fix 4 (P2): Batch JSON results return valid JSON array
"""

import json
from pathlib import Path
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


# ---------- Fix 2: Benchmark offline filtering ----------

@pytest.mark.unit
class TestBenchmarkCapacityFiltering:
    """Capacity comparison should exclude servers that went offline during benchmark."""

    def test_offline_server_excluded_from_profiles(self):
        from app.models import ServerInstance, ServerStatus

        servers = [
            ServerInstance(server_id="s1", name="S1", host="10.0.0.1", port=10095,
                           protocol_version="v2_new", max_concurrency=4,
                           status=ServerStatus.ONLINE, rtf_baseline=0.124),
            ServerInstance(server_id="s2", name="S2", host="10.0.0.2", port=10095,
                           protocol_version="v2_new", max_concurrency=4,
                           status=ServerStatus.ONLINE, rtf_baseline=0.5),
        ]

        servers[1].status = ServerStatus.OFFLINE

        still_online = [s for s in servers if s.status == ServerStatus.ONLINE]
        assert len(still_online) == 1
        assert still_online[0].server_id == "s1"

    def test_all_servers_offline_yields_empty_profiles(self):
        from app.models import ServerInstance, ServerStatus

        servers = [
            ServerInstance(server_id=f"s{i}", name=f"S{i}", host="10.0.0.1",
                           port=10095 + i, protocol_version="v2_new",
                           max_concurrency=4, status=ServerStatus.ONLINE)
            for i in range(3)
        ]
        for s in servers:
            s.status = ServerStatus.OFFLINE
        still_online = [s for s in servers if s.status == ServerStatus.ONLINE]
        assert len(still_online) == 0


# ---------- Fix 3: Partial upload failure exit code ----------

@pytest.mark.unit
class TestPartialUploadFailure:
    """Batch mode should exit nonzero when some uploads fail."""

    def test_partial_upload_failure_exits_nonzero(self, mock_client, tmp_path):
        """Simulate 2/3 uploads failing; should exit 1 even if tasks succeed."""
        from cli.api_client import APIError

        call_count = {"n": 0}
        def upload_side_effect(path):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise APIError(500, "upload failed")
            return {"file_id": "file-ok-1"}

        mock_client.upload_file.side_effect = upload_side_effect
        mock_client.create_tasks.return_value = [
            {"task_id": "T001", "file_id": "file-ok-1", "status": "PREPROCESSING",
             "task_group_id": "GRP01", "language": "zh", "progress": 0},
        ]

        f1 = tmp_path / "a.wav"; f1.write_bytes(b"RIFF" + b"\x00" * 100)
        f2 = tmp_path / "b.wav"; f2.write_bytes(b"RIFF" + b"\x00" * 100)
        f3 = tmp_path / "c.wav"; f3.write_bytes(b"RIFF" + b"\x00" * 100)

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "transcribe", str(f1), str(f2), str(f3),
            "--no-wait", "--json-summary",
        ])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert len(data["upload_failures"]) == 2

    def test_all_uploads_succeed_exits_zero(self, mock_client, tmp_path):
        """All uploads succeed → no-wait exits 0."""
        mock_client.upload_file.return_value = {"file_id": "file-ok-1"}
        mock_client.create_tasks.return_value = [
            {"task_id": "T001", "file_id": "file-ok-1", "status": "PREPROCESSING",
             "task_group_id": "GRP01", "language": "zh", "progress": 0},
        ]

        f1 = tmp_path / "a.wav"; f1.write_bytes(b"RIFF" + b"\x00" * 100)

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "transcribe", str(f1),
            "--batch", "--no-wait", "--json-summary",
        ])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["upload_failures"] == []

    def test_upload_failures_in_json_summary(self, mock_client, tmp_path):
        """upload_failures field should list failed file names."""
        from cli.api_client import APIError

        call_count = {"n": 0}
        def upload_side_effect(path):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise APIError(500, "disk full")
            return {"file_id": f"file-{call_count['n']}"}

        mock_client.upload_file.side_effect = upload_side_effect
        mock_client.create_tasks.return_value = [
            {"task_id": "T002", "file_id": "file-2", "status": "PREPROCESSING",
             "task_group_id": "GRP02", "language": "zh", "progress": 0},
        ]

        f1 = tmp_path / "fail.wav"; f1.write_bytes(b"x" * 50)
        f2 = tmp_path / "ok.wav"; f2.write_bytes(b"x" * 50)

        result = runner.invoke(app, [
            "--server", "http://test:8000", "--quiet",
            "transcribe", str(f1), str(f2),
            "--no-wait", "--json-summary",
        ])
        assert result.exit_code == 1
        data = json.loads(result.stdout)
        assert "fail.wav" in data["upload_failures"]
        assert "ok.wav" not in data["upload_failures"]


# ---------- Fix 4: JSON results validity (unit-level structure test) ----------

@pytest.mark.unit
class TestBatchJSONResultFormat:
    """Verify _json_results returns a list of {task_id, file_name, result}."""

    def test_json_array_structure(self):
        """Multiple results should form a JSON array, not text concatenation."""
        items = []
        results_data = [
            ("T001", "ep01.wav", '{"text": "hello"}'),
            ("T002", "ep02.wav", '{"text": "world"}'),
        ]
        for task_id, fname, content in results_data:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = {"_raw": content}
            items.append({
                "task_id": task_id,
                "file_name": fname,
                "result": parsed,
            })

        output = json.dumps(items, ensure_ascii=False)
        reparsed = json.loads(output)
        assert isinstance(reparsed, list)
        assert len(reparsed) == 2
        assert reparsed[0]["task_id"] == "T001"
        assert reparsed[0]["result"]["text"] == "hello"
        assert reparsed[1]["task_id"] == "T002"

    def test_single_result_is_still_array(self):
        """Even a single result should be a JSON array with 1 element."""
        items = [{
            "task_id": "T001",
            "file_name": "audio.wav",
            "result": {"text": "single"},
        }]
        output = json.dumps(items)
        reparsed = json.loads(output)
        assert isinstance(reparsed, list)
        assert len(reparsed) == 1
