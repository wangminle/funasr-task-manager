"""Health endpoint smoke tests."""

import pytest


@pytest.mark.unit
async def test_health_returns_200(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data


@pytest.mark.unit
async def test_metrics_returns_prometheus_format(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "asr_tasks_total" in response.text
