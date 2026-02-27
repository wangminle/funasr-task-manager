"""ASR server probe module - 3-level probing with cache."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum

import websockets

from app.adapters.base import ServerType
from app.observability.logging import get_logger

logger = get_logger(__name__)

PROBE_CACHE_TTL = 86400  # 24 hours


class ProbeLevel(StrEnum):
    CONNECT_ONLY = "connect_only"
    OFFLINE_LIGHT = "offline_light"
    FULL_2PASS = "full_2pass"


@dataclass
class ServerCapabilities:
    server_id: str
    host: str
    port: int
    is_reachable: bool = False
    inferred_server_type: ServerType = ServerType.AUTO
    supported_modes: list[str] = field(default_factory=list)
    has_timestamp: bool = False
    has_itn: bool = True
    is_final_semantics: str = "unknown"
    probe_level: ProbeLevel = ProbeLevel.CONNECT_ONLY
    probe_time: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "host": self.host,
            "port": self.port,
            "is_reachable": self.is_reachable,
            "inferred_server_type": self.inferred_server_type.value,
            "supported_modes": self.supported_modes,
            "has_timestamp": self.has_timestamp,
            "has_itn": self.has_itn,
            "is_final_semantics": self.is_final_semantics,
            "probe_level": self.probe_level.value,
            "probe_time": self.probe_time,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ServerCapabilities":
        return cls(
            server_id=data["server_id"],
            host=data["host"],
            port=data["port"],
            is_reachable=data.get("is_reachable", False),
            inferred_server_type=ServerType(data.get("inferred_server_type", "auto")),
            supported_modes=data.get("supported_modes", []),
            has_timestamp=data.get("has_timestamp", False),
            has_itn=data.get("has_itn", True),
            is_final_semantics=data.get("is_final_semantics", "unknown"),
            probe_level=ProbeLevel(data.get("probe_level", "connect_only")),
            probe_time=data.get("probe_time", 0.0),
            error=data.get("error"),
        )


class ProbeCache:
    """In-memory probe cache with TTL."""

    def __init__(self, ttl: int = PROBE_CACHE_TTL):
        self._cache: dict[str, tuple[float, ServerCapabilities]] = {}
        self._ttl = ttl

    def get(self, server_id: str) -> ServerCapabilities | None:
        entry = self._cache.get(server_id)
        if entry is None:
            return None
        ts, caps = entry
        if time.monotonic() - ts >= self._ttl:
            del self._cache[server_id]
            return None
        return caps

    def put(self, server_id: str, caps: ServerCapabilities) -> None:
        self._cache[server_id] = (time.monotonic(), caps)

    def invalidate(self, server_id: str) -> None:
        self._cache.pop(server_id, None)

    def clear(self) -> None:
        self._cache.clear()


_probe_cache = ProbeCache()


def get_probe_cache() -> ProbeCache:
    return _probe_cache


# Level ordering for cache hit: higher level = more info, can satisfy lower requests
_PROBE_LEVEL_ORDER = {ProbeLevel.CONNECT_ONLY: 0, ProbeLevel.OFFLINE_LIGHT: 1, ProbeLevel.FULL_2PASS: 2}


class ServerProbe:
    """3-level ASR server probe."""

    def __init__(self, use_ssl: bool = False, connect_timeout: float = 5.0):
        self._use_ssl = use_ssl
        self._connect_timeout = connect_timeout

    async def probe(
        self,
        server_id: str,
        host: str,
        port: int,
        level: ProbeLevel = ProbeLevel.OFFLINE_LIGHT,
    ) -> ServerCapabilities:
        cache = get_probe_cache()
        cached = cache.get(server_id)
        if cached is not None and _PROBE_LEVEL_ORDER.get(cached.probe_level, 0) >= _PROBE_LEVEL_ORDER.get(level, 0):
            logger.debug("probe_cache_hit", server_id=server_id, level=level.value)
            return cached

        caps = ServerCapabilities(server_id=server_id, host=host, port=port)
        start = time.monotonic()

        try:
            if level == ProbeLevel.CONNECT_ONLY:
                caps = await self._probe_connect(caps)
            elif level == ProbeLevel.OFFLINE_LIGHT:
                caps = await self._probe_connect(caps)
                if caps.is_reachable:
                    caps = await self._probe_offline_light(caps)
            else:
                caps = await self._probe_connect(caps)
                if caps.is_reachable:
                    caps = await self._probe_offline_light(caps)
        except Exception as e:
            caps.error = str(e)
            logger.error("probe_failed", server_id=server_id, error=str(e))

        caps.probe_time = time.monotonic() - start
        caps.probe_level = level
        cache.put(server_id, caps)
        return caps

    async def _probe_connect(self, caps: ServerCapabilities) -> ServerCapabilities:
        """Level 1: TCP/WebSocket connection test only."""
        scheme = "wss" if self._use_ssl else "ws"
        uri = f"{scheme}://{caps.host}:{caps.port}"
        try:
            async with asyncio.timeout(self._connect_timeout):
                async with websockets.connect(uri, close_timeout=2):
                    caps.is_reachable = True
                    logger.info("probe_connect_ok", server_id=caps.server_id, uri=uri)
        except Exception as e:
            caps.is_reachable = False
            caps.error = f"Connection failed: {e}"
            logger.warning("probe_connect_failed", server_id=caps.server_id, error=str(e))
        return caps

    async def _probe_offline_light(self, caps: ServerCapabilities) -> ServerCapabilities:
        """Level 2: Send a tiny silent WAV, check response to infer server type."""
        scheme = "wss" if self._use_ssl else "ws"
        uri = f"{scheme}://{caps.host}:{caps.port}"

        silent_wav = b'\x00' * 3200  # 0.1s of 16kHz 16-bit mono silence
        start_msg = json.dumps({
            "mode": "offline",
            "wav_name": "probe_test",
            "wav_format": "pcm",
            "audio_fs": 16000,
            "is_speaking": True,
            "itn": True,
        })
        end_msg = json.dumps({"is_speaking": False})

        try:
            async with asyncio.timeout(15.0):
                async with websockets.connect(uri, max_size=None, close_timeout=3) as ws:
                    await ws.send(start_msg)
                    await ws.send(silent_wav)
                    await ws.send(end_msg)

                    caps.supported_modes.append("offline")

                    async for msg in ws:
                        if isinstance(msg, bytes):
                            continue
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue

                        is_final = data.get("is_final", None)
                        mode = data.get("mode", "")

                        if data.get("timestamp") is not None:
                            caps.has_timestamp = True
                        if data.get("stamp_sents") is not None:
                            caps.has_timestamp = True

                        if is_final is True or (isinstance(is_final, str) and is_final.lower() == "true"):
                            caps.is_final_semantics = "standard"
                            caps.inferred_server_type = ServerType.LEGACY
                        elif is_final is False or (isinstance(is_final, str) and is_final.lower() == "false"):
                            if mode.lower() == "offline":
                                caps.is_final_semantics = "inverted_in_offline"
                                caps.inferred_server_type = ServerType.FUNASR_MAIN
                        break

            logger.info(
                "probe_offline_light_ok",
                server_id=caps.server_id,
                inferred_type=caps.inferred_server_type.value,
                is_final_semantics=caps.is_final_semantics,
            )
        except Exception as e:
            caps.error = f"Offline light probe failed: {e}"
            logger.warning("probe_offline_light_failed", server_id=caps.server_id, error=str(e))

        return caps
