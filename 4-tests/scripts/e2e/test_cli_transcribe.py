"""E2E test for asr-cli transcribe command.

Requires a running ASR backend: set ASR_E2E_SERVER env var.
Mark: pytest -m e2e
"""

import os
import json
import pytest
from pathlib import Path
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()

pytestmark = [pytest.mark.e2e, pytest.mark.slow]

E2E_SERVER = os.environ.get("ASR_E2E_SERVER", "http://localhost:15797")
E2E_API_KEY = os.environ.get("ASR_E2E_API_KEY", "")


@pytest.fixture
def test_audio(fixtures_dir):
    """Locate a small test audio file in fixtures."""
    candidates = list(fixtures_dir.glob("*.wav")) + list(fixtures_dir.glob("*.mp3"))
    if not candidates:
        pytest.skip("No test audio files in fixtures/")
    return candidates[0]


class TestTranscribeE2E:
    def test_transcribe_full_pipeline(self, test_audio, tmp_path):
        args = [
            "--server", E2E_SERVER,
            "--output", "json",
            "--quiet",
            "transcribe",
            str(test_audio),
            "--language", "zh",
            "--format", "json",
            "--output-dir", str(tmp_path),
            "--timeout", "300",
        ]
        if E2E_API_KEY:
            args.extend(["--api-key", E2E_API_KEY])
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"CLI failed: {result.stdout}"

        data = json.loads(result.stdout)
        if isinstance(data, list):
            data = data[0]
        assert data["status"] == "SUCCEEDED"
        assert "output" in data

    def test_transcribe_srt_format(self, test_audio, tmp_path):
        args = [
            "--server", E2E_SERVER,
            "--quiet",
            "transcribe",
            str(test_audio),
            "--format", "srt",
            "--output-dir", str(tmp_path),
        ]
        if E2E_API_KEY:
            args.extend(["--api-key", E2E_API_KEY])
        result = runner.invoke(app, args)
        assert result.exit_code == 0

    def test_transcribe_nonexistent_file(self, tmp_path):
        result = runner.invoke(app, [
            "--server", E2E_SERVER,
            "transcribe",
            str(tmp_path / "nonexistent.wav"),
        ])
        assert result.exit_code == 1
