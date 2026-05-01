"""Unit tests for CLI API client with mocked HTTP."""

import json
import pytest
from unittest.mock import MagicMock, patch

import httpx

from cli.api_client import ASRClient, APIError


@pytest.fixture
def mock_client():
    """Create ASRClient with a mocked httpx.Client."""
    client = ASRClient("http://test:15797")
    client._client = MagicMock(spec=httpx.Client)
    return client


def _make_response(status_code: int = 200, json_data=None, text: str = ""):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.text = text
    return resp


class TestHealth:
    def test_health_ok(self, mock_client):
        mock_client._client.get.return_value = _make_response(json_data={"status": "ok"})
        result = mock_client.health()
        assert result["status"] == "ok"
        mock_client._client.get.assert_called_with("/health")

    def test_health_error(self, mock_client):
        mock_client._client.get.return_value = _make_response(500, json_data={"detail": "down"})
        with pytest.raises(APIError) as exc:
            mock_client.health()
        assert exc.value.status_code == 500


class TestStats:
    def test_stats_ok(self, mock_client):
        data = {"server_total": 2, "server_online": 1, "queue_depth": 5}
        mock_client._client.get.return_value = _make_response(json_data=data)
        result = mock_client.stats()
        assert result["server_total"] == 2


class TestTasks:
    def test_list_tasks(self, mock_client):
        data = {"items": [], "total": 0, "page": 1, "page_size": 20}
        mock_client._client.get.return_value = _make_response(json_data=data)
        result = mock_client.list_tasks(status="SUCCEEDED", search="test", page=1, page_size=10)
        assert result["total"] == 0

    def test_create_tasks(self, mock_client):
        tasks = [{"task_id": "T1", "status": "PREPROCESSING"}]
        mock_client._client.post.return_value = _make_response(201, json_data=tasks)
        result = mock_client.create_tasks([{"file_id": "F1", "language": "zh"}])
        assert len(result) == 1

    def test_cancel_task(self, mock_client):
        mock_client._client.post.return_value = _make_response(json_data={"task_id": "T1", "status": "CANCELED"})
        result = mock_client.cancel_task("T1")
        assert result["status"] == "CANCELED"

    def test_get_result(self, mock_client):
        mock_client._client.get.return_value = _make_response(text='{"text": "hello"}')
        result = mock_client.get_result("T1", fmt="json")
        assert "hello" in result


class TestServers:
    def test_list_servers(self, mock_client):
        mock_client._client.get.return_value = _make_response(json_data=[])
        assert mock_client.list_servers() == []

    def test_register_server(self, mock_client):
        data = {"server_id": "s1", "status": "ONLINE"}
        mock_client._client.post.return_value = _make_response(json_data=data)
        result = mock_client.register_server({"server_id": "s1", "host": "h", "port": 10095})
        assert result["server_id"] == "s1"

    def test_delete_server(self, mock_client):
        mock_client._client.delete.return_value = _make_response(204)
        mock_client.delete_server("s1")


class TestFiles:
    def test_upload_file(self, mock_client, tmp_path):
        test_file = tmp_path / "test.wav"
        test_file.write_bytes(b"fake audio data")
        mock_client._client.post.return_value = _make_response(json_data={"file_id": "F1", "status": "READY"})
        result = mock_client.upload_file(test_file)
        assert result["file_id"] == "F1"

    def test_file_info(self, mock_client):
        mock_client._client.get.return_value = _make_response(json_data={"file_id": "F1", "original_name": "a.wav"})
        result = mock_client.file_info("F1")
        assert result["original_name"] == "a.wav"
