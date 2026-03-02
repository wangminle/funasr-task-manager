"""Heartbeat service unit tests."""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

from app.models.server import ServerStatus
from app.services.heartbeat import HeartbeatService
from app.services.server_probe import ServerCapabilities


@pytest.mark.unit
class TestHeartbeatService:
    async def test_online_server_stays_online(self):
        """Reachable server should remain ONLINE."""
        svc = HeartbeatService(interval=1, timeout=60)
        caps = ServerCapabilities(reachable=True)

        with pytest.MonkeyPatch.context() as m:
            m.setattr(svc, "_probe_with_ssl_fallback", AsyncMock(return_value=caps))
            update_fn = AsyncMock()
            server = {"server_id": "s1", "host": "10.0.0.1", "port": 10095, "status": "ONLINE", "last_heartbeat": datetime.now(timezone.utc)}
            await svc._check_one(server, update_fn)
            update_fn.assert_called_once()
            assert update_fn.call_args[0][1] == ServerStatus.ONLINE

    async def test_unreachable_with_expired_heartbeat_goes_offline(self):
        """T-M2-06: Unreachable > timeout_seconds -> OFFLINE."""
        svc = HeartbeatService(interval=1, timeout=60)
        caps = ServerCapabilities(reachable=False, error="refused")

        old_hb = datetime.now(timezone.utc) - timedelta(seconds=120)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(svc, "_probe_with_ssl_fallback", AsyncMock(return_value=caps))
            update_fn = AsyncMock()
            server = {"server_id": "s1", "host": "10.0.0.1", "port": 10095, "status": "ONLINE", "last_heartbeat": old_hb}
            await svc._check_one(server, update_fn)
            update_fn.assert_called_once()
            assert update_fn.call_args[0][1] == ServerStatus.OFFLINE

    async def test_unreachable_within_timeout_goes_degraded(self):
        """Unreachable but within timeout -> DEGRADED."""
        svc = HeartbeatService(interval=1, timeout=60)
        caps = ServerCapabilities(reachable=False, error="timeout")

        recent_hb = datetime.now(timezone.utc) - timedelta(seconds=10)
        with pytest.MonkeyPatch.context() as m:
            m.setattr(svc, "_probe_with_ssl_fallback", AsyncMock(return_value=caps))
            update_fn = AsyncMock()
            server = {"server_id": "s1", "host": "10.0.0.1", "port": 10095, "status": "ONLINE", "last_heartbeat": recent_hb}
            await svc._check_one(server, update_fn)
            update_fn.assert_called_once()
            assert update_fn.call_args[0][1] == ServerStatus.DEGRADED
