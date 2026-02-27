"""Prometheus metrics integration tests (T-M3-30, T-M3-31, T-M3-32)."""

import pytest


@pytest.mark.integration
class TestPrometheusMetrics:
    async def test_metrics_endpoint_returns_prometheus_format(self, client):
        """T-M3-30: GET /metrics returns Prometheus text format."""
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text
        assert "asr_tasks_total" in text or "# HELP" in text

    async def test_metrics_include_circuit_breaker(self, client):
        """T-M3-32: Circuit breaker state metric is registered."""
        from app.observability.metrics import asr_circuit_breaker_state
        asr_circuit_breaker_state.labels(server_id="test-s1").set(0)

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "asr_circuit_breaker_state" in resp.text

    async def test_metrics_include_retry_counter(self, client):
        """T-M3-31 variant: retry metric is registered."""
        from app.observability.metrics import asr_task_retries_total
        asr_task_retries_total.inc()

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "asr_task_retries_total" in resp.text

    async def test_metrics_include_rate_limit_counter(self, client):
        from app.observability.metrics import asr_rate_limit_rejections_total
        asr_rate_limit_rejections_total.labels(dimension="concurrent").inc()

        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "asr_rate_limit_rejections_total" in resp.text
