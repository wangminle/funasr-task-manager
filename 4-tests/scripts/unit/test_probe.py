"""Server probe unit tests."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.base import ServerType
from app.services.probe import (
    ProbeCache, ProbeLevel, ServerCapabilities, ServerProbe, get_probe_cache,
)


@pytest.mark.unit
class TestProbeCache:
    def test_cache_put_and_get(self):
        cache = ProbeCache(ttl=3600)
        caps = ServerCapabilities(server_id="s1", host="10.0.0.1", port=10095, is_reachable=True)
        cache.put("s1", caps)
        result = cache.get("s1")
        assert result is not None
        assert result.is_reachable is True

    def test_cache_miss(self):
        cache = ProbeCache(ttl=3600)
        assert cache.get("nonexistent") is None

    def test_cache_expired(self):
        cache = ProbeCache(ttl=0)
        caps = ServerCapabilities(server_id="s1", host="10.0.0.1", port=10095)
        cache.put("s1", caps)
        time.sleep(0.01)
        assert cache.get("s1") is None

    def test_cache_invalidate(self):
        cache = ProbeCache(ttl=3600)
        caps = ServerCapabilities(server_id="s1", host="10.0.0.1", port=10095)
        cache.put("s1", caps)
        cache.invalidate("s1")
        assert cache.get("s1") is None

    def test_cache_clear(self):
        cache = ProbeCache(ttl=3600)
        for i in range(5):
            caps = ServerCapabilities(server_id=f"s{i}", host="10.0.0.1", port=10095)
            cache.put(f"s{i}", caps)
        cache.clear()
        for i in range(5):
            assert cache.get(f"s{i}") is None


@pytest.mark.unit
class TestServerCapabilities:
    def test_to_dict_and_from_dict(self):
        caps = ServerCapabilities(
            server_id="test-01", host="192.168.1.1", port=10095,
            is_reachable=True, inferred_server_type=ServerType.FUNASR_MAIN,
            supported_modes=["offline", "online"], has_timestamp=True,
            is_final_semantics="inverted_in_offline",
            probe_level=ProbeLevel.OFFLINE_LIGHT, probe_time=1.5,
        )
        d = caps.to_dict()
        assert d["server_id"] == "test-01"
        assert d["inferred_server_type"] == "funasr_main"

        restored = ServerCapabilities.from_dict(d)
        assert restored.server_id == "test-01"
        assert restored.inferred_server_type == ServerType.FUNASR_MAIN
        assert "offline" in restored.supported_modes


async def _async_iter(items):
    for item in items:
        yield item


@pytest.mark.unit
class TestServerProbe:
    async def test_probe_connect_only_success(self):
        """T-M2-01: connect-only probe should complete in < 1s."""
        probe = ServerProbe(use_ssl=False, connect_timeout=2.0)

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.probe.websockets.connect", return_value=mock_ws):
            with patch.object(get_probe_cache(), "get", return_value=None):
                caps = await probe.probe("test-01", "10.0.0.1", 10095, level=ProbeLevel.CONNECT_ONLY)

        assert caps.is_reachable is True
        assert caps.probe_time < 1.0

    async def test_probe_connect_only_failure(self):
        probe = ServerProbe(use_ssl=False, connect_timeout=1.0)

        with patch("app.services.probe.websockets.connect", side_effect=ConnectionRefusedError("refused")):
            with patch.object(get_probe_cache(), "get", return_value=None):
                caps = await probe.probe("fail-01", "10.0.0.1", 10095, level=ProbeLevel.CONNECT_ONLY)

        assert caps.is_reachable is False
        assert caps.error is not None

    async def test_probe_offline_light_infer_new_server(self):
        """T-M2-02: offline-light probe should correctly infer server_type."""
        probe = ServerProbe(use_ssl=False)

        response_data = json.dumps({"text": "", "mode": "offline", "is_final": False})

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)
        mock_ws.send = AsyncMock()
        mock_ws.__aiter__ = lambda *a, **kw: _async_iter([response_data])

        with patch("app.services.probe.websockets.connect", return_value=mock_ws):
            with patch.object(get_probe_cache(), "get", return_value=None):
                caps = await probe.probe("new-01", "10.0.0.1", 10095, level=ProbeLevel.OFFLINE_LIGHT)

        assert caps.inferred_server_type == ServerType.FUNASR_MAIN
        assert caps.is_final_semantics == "inverted_in_offline"

    async def test_probe_offline_light_infer_legacy_server(self):
        probe = ServerProbe(use_ssl=False)

        response_data = json.dumps({"text": "测试", "mode": "offline", "is_final": True})

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)
        mock_ws.send = AsyncMock()
        mock_ws.__aiter__ = lambda *a, **kw: _async_iter([response_data])

        with patch("app.services.probe.websockets.connect", return_value=mock_ws):
            with patch.object(get_probe_cache(), "get", return_value=None):
                caps = await probe.probe("old-01", "10.0.0.1", 10095, level=ProbeLevel.OFFLINE_LIGHT)

        assert caps.inferred_server_type == ServerType.LEGACY
        assert caps.is_final_semantics == "standard"

    async def test_cache_hit_skips_probe(self):
        """T-M2-03: cache hit should not trigger new probe."""
        probe = ServerProbe(use_ssl=False)
        cached = ServerCapabilities(
            server_id="cached-01", host="10.0.0.1", port=10095,
            is_reachable=True, probe_level=ProbeLevel.OFFLINE_LIGHT,
        )

        with patch.object(get_probe_cache(), "get", return_value=cached):
            caps = await probe.probe("cached-01", "10.0.0.1", 10095, level=ProbeLevel.CONNECT_ONLY)
            assert caps.server_id == "cached-01"
            assert caps.is_reachable is True
