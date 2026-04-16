"""Server management API integration tests for P1-1.

Tests probe, benchmark, update endpoints, and NDJSON streaming contracts.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


async def _register_server(client, server_id: str = "asr-p1-01", port: int = 10095):
    body = {
        "server_id": server_id,
        "name": f"Test {server_id}",
        "host": "203.0.113.14",
        "port": port,
        "protocol_version": "v2_new",
        "max_concurrency": 4,
    }
    resp = await client.post("/api/v1/servers", json=body)
    assert resp.status_code in (201, 409)
    return server_id


def _parse_ndjson(text: str) -> list[dict]:
    """Parse NDJSON text into a list of dicts."""
    events = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


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


def _make_mock_benchmark_result():
    """Create a mock ServerBenchmarkResult for testing."""
    from app.services.server_benchmark import ServerBenchmarkResult
    return ServerBenchmarkResult(
        reachable=True,
        responsive=True,
        single_rtf=0.14,
        throughput_rtf=0.08,
        benchmark_concurrency=2,
        recommended_concurrency=2,
        benchmark_audio_sec=300.0,
        benchmark_elapsed_sec=37.2,
        benchmark_samples=["test.mp4"],
        benchmark_notes=[],
        gradient_complete=True,
    )


def _make_mock_probe_caps():
    """Create a mock ServerCapabilities for testing."""
    from app.services.server_probe import ServerCapabilities
    return ServerCapabilities(
        reachable=True,
        responsive=True,
        inferred_server_type="funasr_main",
        supports_offline=True,
        supports_2pass=False,
        supports_online=False,
        probe_duration_ms=50.0,
    )


@pytest.mark.integration
class TestNDJSONStreamingContract:
    """Verify NDJSON streaming contract for benchmark and register+benchmark endpoints."""

    async def test_register_with_benchmark_returns_ndjson(self, client):
        """POST /servers with run_benchmark=true returns NDJSON stream."""
        bench_result = _make_mock_benchmark_result()
        probe_caps = _make_mock_probe_caps()

        async def mock_benchmark(host, port, *, timeout=900.0, progress_callback=None, use_ssl=True):
            if progress_callback:
                await progress_callback({"type": "benchmark_start", "total_phases": 2, "samples": ["test.mp4"]})
                await progress_callback({"type": "phase_complete", "phase": 1, "single_rtf": 0.14})
                await progress_callback({"type": "benchmark_complete", "recommended_concurrency": 2})
            return bench_result

        async def mock_probe(host, port, *, use_ssl=True, level=None, timeout=8.0):
            return probe_caps

        with (
            patch("app.api.servers.benchmark_server_full_with_ssl_fallback", side_effect=mock_benchmark),
            patch("app.api.servers.probe_server", side_effect=mock_probe),
        ):
            body = {
                "server_id": "asr-ndjson-01",
                "name": "NDJSON Test",
                "host": "10.0.0.99",
                "port": 10095,
                "protocol_version": "v2_new",
                "max_concurrency": 4,
                "run_benchmark": True,
            }
            resp = await client.post("/api/v1/servers", json=body)
            assert resp.status_code == 201
            assert "application/x-ndjson" in resp.headers.get("content-type", "")

            events = _parse_ndjson(resp.text)
            assert len(events) >= 2
            assert events[0]["type"] == "server_registered"
            assert events[0]["server_id"] == "asr-ndjson-01"

            final_types = [e["type"] for e in events]
            assert "benchmark_result" in final_types

    async def test_single_benchmark_returns_ndjson(self, client):
        """POST /servers/{id}/benchmark returns NDJSON stream with progress events."""
        sid = await _register_server(client, "asr-bench-ndjson-01")
        bench_result = _make_mock_benchmark_result()

        async def mock_benchmark(host, port, *, timeout=900.0, progress_callback=None, use_ssl=True):
            if progress_callback:
                await progress_callback({"type": "benchmark_start", "total_phases": 2, "samples": ["test.mp4"]})
                await progress_callback({"type": "phase_start", "phase": 1, "description": "单线程测速"})
                await progress_callback({"type": "phase_complete", "phase": 1, "single_rtf": 0.14})
            return bench_result

        with patch("app.api.servers.benchmark_server_full_with_ssl_fallback", side_effect=mock_benchmark):
            resp = await client.post(f"/api/v1/servers/{sid}/benchmark")
            assert resp.status_code == 200
            assert "application/x-ndjson" in resp.headers.get("content-type", "")

            events = _parse_ndjson(resp.text)
            event_types = [e["type"] for e in events]
            assert "benchmark_start" in event_types
            assert "benchmark_result" in event_types

            final = next(e for e in events if e["type"] == "benchmark_result")
            assert "data" in final

    async def test_batch_benchmark_returns_ndjson(self, client):
        """POST /servers/benchmark returns NDJSON stream ending with all_complete."""
        probe_caps = _make_mock_probe_caps()

        async def mock_probe(host, port, *, use_ssl=True, level=None, timeout=8.0):
            return probe_caps

        with patch("app.api.servers.probe_server", side_effect=mock_probe):
            await _register_server(client, "asr-batch-ndjson-01", port=10095)

        bench_result = _make_mock_benchmark_result()

        async def mock_benchmark(host, port, *, timeout=900.0, progress_callback=None, use_ssl=True):
            if progress_callback:
                await progress_callback({"type": "benchmark_start", "total_phases": 2, "samples": ["test.mp4"]})
            return bench_result

        with patch("app.api.servers.benchmark_server_full_with_ssl_fallback", side_effect=mock_benchmark):
            resp = await client.post("/api/v1/servers/benchmark")
            assert resp.status_code == 200
            assert "application/x-ndjson" in resp.headers.get("content-type", "")

            events = _parse_ndjson(resp.text)
            event_types = [e["type"] for e in events]
            assert "all_benchmark_start" in event_types
            assert "all_complete" in event_types

            final = next(e for e in events if e["type"] == "all_complete")
            assert "results" in final["data"]
            assert "capacity_comparison" in final["data"]
