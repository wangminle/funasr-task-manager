"""Heartbeat service - periodic server health checks."""

import asyncio
from datetime import datetime, timezone

from app.config import settings
from app.models.server import ServerStatus
from app.services.probe import ServerProbe, ProbeLevel, get_probe_cache
from app.observability.logging import get_logger

logger = get_logger(__name__)


class HeartbeatService:
    """Periodically checks ASR server health via connect-only probe."""

    def __init__(
        self,
        interval: int | None = None,
        timeout: int | None = None,
    ):
        self._interval = interval or settings.heartbeat_interval_seconds
        self._timeout = timeout or settings.heartbeat_timeout_seconds
        self._probe = ServerProbe(use_ssl=False, connect_timeout=5.0)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self, get_servers_fn, update_status_fn) -> None:
        """Start the heartbeat loop.

        get_servers_fn: async () -> list[dict] with server_id, host, port, status, last_heartbeat
        update_status_fn: async (server_id, new_status, last_heartbeat) -> None
        """
        self._running = True
        self._task = asyncio.create_task(
            self._loop(get_servers_fn, update_status_fn)
        )
        logger.info("heartbeat_started", interval=self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("heartbeat_stopped")

    async def _loop(self, get_servers_fn, update_status_fn) -> None:
        while self._running:
            try:
                servers = await get_servers_fn()
                for srv in servers:
                    await self._check_one(srv, update_status_fn)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat_loop_error", error=str(e))
            await asyncio.sleep(self._interval)

    async def _check_one(self, server: dict, update_status_fn) -> None:
        server_id = server["server_id"]
        host = server["host"]
        port = server["port"]
        current_status = server.get("status", "OFFLINE")

        get_probe_cache().invalidate(server_id)
        caps = await self._probe.probe(server_id, host, port, level=ProbeLevel.CONNECT_ONLY)

        now = datetime.now(timezone.utc)

        if caps.is_reachable:
            if current_status != ServerStatus.ONLINE:
                logger.info("server_back_online", server_id=server_id)
            await update_status_fn(server_id, ServerStatus.ONLINE, now)
        else:
            last_hb = server.get("last_heartbeat")
            if last_hb and isinstance(last_hb, datetime):
                if last_hb.tzinfo is None:
                    last_hb = last_hb.replace(tzinfo=timezone.utc)
                elapsed = (now - last_hb).total_seconds()
                if elapsed > self._timeout:
                    if current_status != ServerStatus.OFFLINE:
                        logger.warning("server_heartbeat_timeout", server_id=server_id, elapsed=elapsed)
                    await update_status_fn(server_id, ServerStatus.OFFLINE, None)
                else:
                    if current_status == ServerStatus.ONLINE:
                        await update_status_fn(server_id, ServerStatus.DEGRADED, None)
            else:
                await update_status_fn(server_id, ServerStatus.OFFLINE, None)


heartbeat_service = HeartbeatService()
