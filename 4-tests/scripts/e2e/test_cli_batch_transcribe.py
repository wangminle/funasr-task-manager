"""E2E test for CLI batch transcribe and task-group commands.

Requires a running ASR backend: set ASR_E2E_SERVER env var.
Uses the remote-standard approach: tests talk to a real backend via CLI.

Mark: pytest -m e2e
"""

import json
import os
import shutil

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

E2E_SERVER = os.environ.get("ASR_E2E_SERVER", "http://localhost:15797")
E2E_API_KEY = os.environ.get("ASR_E2E_API_KEY", "")


def _base_args() -> list[str]:
    args = ["--server", E2E_SERVER, "--output", "json", "--quiet"]
    if E2E_API_KEY:
        args.extend(["--api-key", E2E_API_KEY])
    return args


@pytest.fixture
def test_audios(fixtures_dir):
    """Locate at least 2 test audio files for batch testing."""
    candidates = list(fixtures_dir.glob("*.wav")) + list(fixtures_dir.glob("*.mp3"))
    if len(candidates) < 2:
        pytest.skip("Need at least 2 test audio files in fixtures/")
    return candidates[:3]


@pytest.fixture
def test_audio(fixtures_dir):
    candidates = list(fixtures_dir.glob("*.wav")) + list(fixtures_dir.glob("*.mp3"))
    if not candidates:
        pytest.skip("No test audio files in fixtures/")
    return candidates[0]


class TestBatchTranscribe:
    """P0-2: CLI batch transcribe mode tests."""

    def test_single_file_backward_compatible(self, test_audio, tmp_path):
        """Single file should still work with the old behavior."""
        args = _base_args() + [
            "transcribe", str(test_audio),
            "--language", "zh", "--format", "txt",
            "--output-dir", str(tmp_path),
            "--timeout", "300",
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"CLI failed: {result.stdout}\n{result.stderr if hasattr(result, 'stderr') else ''}"

    def test_batch_mode_multiple_files(self, test_audios, tmp_path):
        """Multiple files should auto-enable batch mode."""
        file_args = [str(f) for f in test_audios]
        args = _base_args() + [
            "transcribe", *file_args,
            "--language", "zh", "--format", "txt",
            "--output-dir", str(tmp_path),
            "--timeout", "600",
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"Batch CLI failed: {result.stdout}"

    def test_batch_no_wait_returns_immediately(self, test_audios, tmp_path):
        """--no-wait should return immediately with task info."""
        file_args = [str(f) for f in test_audios]
        args = _base_args() + [
            "transcribe", *file_args,
            "--language", "zh",
            "--no-wait",
            "--json-summary",
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"no-wait failed: {result.stdout}"
        data = json.loads(result.stdout)
        assert "task_group_id" in data
        assert "task_ids" in data
        assert len(data["task_ids"]) == len(test_audios)

    def test_nonexistent_files(self, tmp_path):
        """All nonexistent files should fail."""
        args = _base_args() + [
            "transcribe",
            str(tmp_path / "no1.wav"),
            str(tmp_path / "no2.wav"),
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 1


class TestTaskGroupCLI:
    """P0-3: task group CLI commands."""

    def _create_batch_tasks(self, test_audios, tmp_path) -> str:
        """Helper: submit batch and get group_id."""
        file_args = [str(f) for f in test_audios]
        args = _base_args() + [
            "transcribe", *file_args,
            "--language", "zh",
            "--no-wait",
            "--json-summary",
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        return data["task_group_id"]

    def test_task_list_by_group(self, test_audios, tmp_path):
        """task list --group should filter by batch."""
        group_id = self._create_batch_tasks(test_audios, tmp_path)
        args = _base_args() + ["task", "list", "--group", group_id]
        result = runner.invoke(app, args)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["total"] == len(test_audios)

    def test_task_wait_by_group(self, test_audios, tmp_path):
        """task wait --group should wait for all tasks."""
        group_id = self._create_batch_tasks(test_audios, tmp_path)
        args = _base_args() + [
            "task", "wait", "--group", group_id,
            "--timeout", "600",
            "--poll-interval", "3",
        ]
        result = runner.invoke(app, args)
        assert result.exit_code in (0, 1)  # 0 if all succeed, 1 if any fail

    def test_task_delete_by_group(self, test_audios, tmp_path):
        """task delete --group should delete the batch."""
        group_id = self._create_batch_tasks(test_audios, tmp_path)
        args = _base_args() + ["task", "delete", "--group", group_id]
        result = runner.invoke(app, args)
        assert result.exit_code == 0


class TestSystemDoctor:
    """P0-4: system doctor command."""

    def test_doctor_runs(self):
        """doctor should return diagnostics."""
        args = _base_args() + ["doctor"]
        result = runner.invoke(app, args)
        data = json.loads(result.stdout)
        assert "checks" in data
        assert "has_blocking_errors" in data


class TestServerManagement:
    """P1-1: server management CLI commands."""

    def test_server_list(self):
        args = _base_args() + ["server", "list"]
        result = runner.invoke(app, args)
        assert result.exit_code == 0

    def test_server_probe(self):
        """Probe a registered server (register first, then probe)."""
        reg_args = _base_args() + [
            "server", "register",
            "--id", "e2e-probe-target",
            "--host", "203.0.113.30", "--port", "19999",
            "--protocol", "v2_new",
        ]
        runner.invoke(app, reg_args)

        args = _base_args() + ["server", "probe", "e2e-probe-target", "--level", "connect_only"]
        result = runner.invoke(app, args)
        data = json.loads(result.stdout)
        assert "reachable" in data

        runner.invoke(app, _base_args() + ["server", "delete", "e2e-probe-target"])

    def test_server_update(self):
        """Register, update, verify, then clean up."""
        reg_args = _base_args() + [
            "server", "register",
            "--id", "e2e-update-target",
            "--host", "203.0.113.31", "--port", "19998",
            "--protocol", "v2_new", "--max-concurrency", "4",
        ]
        runner.invoke(app, reg_args)

        upd_args = _base_args() + ["server", "update", "e2e-update-target", "--max-concurrency", "12"]
        result = runner.invoke(app, upd_args)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data.get("max_concurrency") == 12

        runner.invoke(app, _base_args() + ["server", "delete", "e2e-update-target"])


class TestBatchResultDelivery:
    """P1-2: batch result delivery enhancements."""

    def _submit_and_wait(self, test_audios, tmp_path) -> str:
        """Submit batch and wait for completion."""
        file_args = [str(f) for f in test_audios]
        submit_args = _base_args() + [
            "transcribe", *file_args,
            "--language", "zh",
            "--no-wait", "--json-summary",
        ]
        result = runner.invoke(app, submit_args)
        data = json.loads(result.stdout)
        group_id = data["task_group_id"]

        wait_args = _base_args() + [
            "task", "wait", "--group", group_id,
            "--timeout", "600", "--poll-interval", "3",
        ]
        runner.invoke(app, wait_args)
        return group_id

    def test_multi_format_download(self, test_audios, tmp_path):
        """Download results in multiple formats with summary."""
        group_id = self._submit_and_wait(test_audios, tmp_path)

        result_dir = tmp_path / "multi_fmt"
        args = _base_args() + [
            "task", "result", "--group", group_id,
            "--format", "txt,json",
            "--output-dir", str(result_dir),
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0

        summary_path = result_dir / "batch-summary.json"
        assert summary_path.exists(), f"batch-summary.json not found in {result_dir}"

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["task_group_id"] == group_id
        assert "txt" in summary["formats_exported"]
        assert "json" in summary["formats_exported"]

    def test_single_format_download(self, test_audios, tmp_path):
        """Download results in a single format."""
        group_id = self._submit_and_wait(test_audios, tmp_path)

        result_dir = tmp_path / "single_fmt"
        args = _base_args() + [
            "task", "result", "--group", group_id,
            "--format", "txt",
            "--output-dir", str(result_dir),
        ]
        result = runner.invoke(app, args)
        assert result.exit_code == 0

        txt_files = list(result_dir.glob("*_result.txt"))
        assert len(txt_files) > 0
