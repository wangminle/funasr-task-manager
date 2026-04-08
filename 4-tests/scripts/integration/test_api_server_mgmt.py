"""Server management API integration tests for P1-1.

Tests probe, benchmark, and update endpoints.
"""

import pytest


async def _register_server(client, server_id: str = "asr-p1-01", port: int = 10095):
    body = {
        "server_id": server_id,
        "name": f"Test {server_id}",
        "host": "192.168.1.100",
        "port": port,
        "protocol_version": "v2_new",
        "max_concurrency": 4,
    }
    resp = await client.post("/api/v1/servers", json=body)
    assert resp.status_code in (201, 409)
    return server_id


@pytest.mark.integration
class TestServerProbeAPI:
    async def test_probe_registered_server(self, client):
        sid = await _register_server(client, "asr-probe-01")
        resp = await client.post(f"/api/v1/servers/{sid}/probe?level=connect_only")
        assert resp.status_code == 200
        data = resp.json()
        assert data["server_id"] == sid
        assert "reachable" in data
        assert "probe_duration_ms" in data

    async def test_probe_nonexistent_server(self, client):
        resp = await client.post("/api/v1/servers/nonexistent/probe")
        assert resp.status_code == 404

@pytest.mark.integration
class TestServerUpdateAPI:
    async def test_update_max_concurrency(self, client):
        sid = await _register_server(client, "asr-update-01")
        resp = await client.patch(f"/api/v1/servers/{sid}", json={"max_concurrency": 8})
        assert resp.status_code == 200
        data = resp.json()
        assert data["max_concurrency"] == 8

    async def test_update_name(self, client):
        sid = await _register_server(client, "asr-update-02")
        resp = await client.patch(f"/api/v1/servers/{sid}", json={"name": "Updated Name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"

    async def test_update_nonexistent(self, client):
        resp = await client.patch("/api/v1/servers/nonexistent", json={"name": "x"})
        assert resp.status_code == 404

    async def test_update_preserves_other_fields(self, client):
        sid = await _register_server(client, "asr-update-03", port=10097)
        await client.patch(f"/api/v1/servers/{sid}", json={"name": "New Name"})
        resp = await client.get("/api/v1/servers")
        servers = resp.json()
        server = next((s for s in servers if s["server_id"] == sid), None)
        assert server is not None
        assert server["port"] == 10097
        assert server["name"] == "New Name"


@pytest.mark.integration
class TestServerBenchmarkAPI:
    async def test_benchmark_no_online_servers(self, client):
        resp = await client.post("/api/v1/servers/benchmark")
        assert resp.status_code == 422

    async def test_single_server_benchmark_nonexistent(self, client):
        resp = await client.post("/api/v1/servers/nonexistent/benchmark")
        assert resp.status_code == 404
