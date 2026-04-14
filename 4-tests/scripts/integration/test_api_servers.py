"""Server management API integration tests."""

import pytest


@pytest.mark.integration
class TestServerAPI:
    async def test_register_server(self, client):
        body = {"server_id": "asr-test-01", "name": "Test Server", "host": "203.0.113.10", "port": 10095, "protocol_version": "v2_new", "max_concurrency": 4, "labels": {"model": "paraformer-zh"}}
        response = await client.post("/api/v1/servers", json=body)
        assert response.status_code == 201
        assert response.json()["server_id"] == "asr-test-01"
        assert response.json()["status"] == "OFFLINE"

    async def test_list_servers(self, client):
        body = {"server_id": "asr-list-01", "host": "203.0.113.11", "port": 10095, "protocol_version": "v2_new"}
        await client.post("/api/v1/servers", json=body)
        response = await client.get("/api/v1/servers")
        assert response.status_code == 200
        assert len(response.json()) >= 1

    async def test_delete_server(self, client):
        body = {"server_id": "asr-delete-01", "host": "203.0.113.12", "port": 10095, "protocol_version": "v1_old"}
        await client.post("/api/v1/servers", json=body)
        response = await client.delete("/api/v1/servers/asr-delete-01")
        assert response.status_code == 204

    async def test_delete_nonexistent_server(self, client):
        response = await client.delete("/api/v1/servers/nonexistent")
        assert response.status_code == 404

    async def test_register_duplicate_server(self, client):
        body = {"server_id": "asr-dup-01", "host": "203.0.113.13", "port": 10095, "protocol_version": "v2_new"}
        await client.post("/api/v1/servers", json=body)
        response = await client.post("/api/v1/servers", json=body)
        assert response.status_code == 409
