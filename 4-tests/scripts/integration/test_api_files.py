"""File upload and metadata API integration tests."""

import io

import pytest


@pytest.mark.integration
class TestFileUploadAPI:
    async def test_upload_wav_returns_201(self, client):
        content = b"RIFF" + b"\x00" * 1000
        files = {"file": ("test.wav", io.BytesIO(content), "audio/wav")}
        response = await client.post("/api/v1/files/upload", files=files)
        assert response.status_code == 201
        data = response.json()
        assert "file_id" in data
        assert data["original_name"] == "test.wav"
        assert len(data["file_id"]) == 26

    async def test_upload_unsupported_format_returns_400(self, client):
        files = {"file": ("malware.exe", io.BytesIO(b"\x00" * 100), "application/octet-stream")}
        response = await client.post("/api/v1/files/upload", files=files)
        assert response.status_code == 400

    async def test_get_file_metadata(self, client):
        content = b"RIFF" + b"\x00" * 500
        files = {"file": ("audio.wav", io.BytesIO(content), "audio/wav")}
        upload_resp = await client.post("/api/v1/files/upload", files=files)
        file_id = upload_resp.json()["file_id"]
        response = await client.get(f"/api/v1/files/{file_id}")
        assert response.status_code == 200
        assert response.json()["file_id"] == file_id

    async def test_get_nonexistent_file_returns_404(self, client):
        response = await client.get("/api/v1/files/nonexistent_id_000000")
        assert response.status_code == 404
