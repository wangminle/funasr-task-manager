"""File manager unit tests."""

import pytest

from app.storage.file_manager import validate_file_extension


@pytest.mark.unit
class TestFileValidation:
    def test_valid_extensions(self):
        for ext in [".wav", ".mp3", ".mp4", ".flac", ".ogg", ".webm", ".m4a"]:
            assert validate_file_extension(f"test{ext}") is True

    def test_invalid_extension(self):
        assert validate_file_extension("malware.exe") is False
        assert validate_file_extension("document.pdf") is False
        assert validate_file_extension("image.png") is False

    def test_case_insensitive(self):
        assert validate_file_extension("test.WAV") is True
        assert validate_file_extension("test.Mp3") is True
