"""FunASR server probe service.

Ported from funasr-client-python's server_probe.py.
Detects server reachability, capabilities, and protocol semantics.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.adapters.websocket_compat import connect_websocket
from app.observability.logging import get_logger

logger = get_logger(__name__)


class ProbeLevel(Enum):
    CONNECT_ONLY = 0
    OFFLINE_LIGHT = 1
    TWOPASS_FULL = 2


@dataclass
class ServerCapabilities:
    reachable: bool = False
    responsive: bool = False
    error: str | None = None

    supports_offline: bool | None = None
    supports_online: bool | None = None
    supports_2pass: bool | None = None

    has_timestamp: bool = False
    has_stamp_sents: bool = False

    is_final_semantics: str = "unknown"
    inferred_server_type: str = "unknown"

    probe_level: ProbeLevel = ProbeLevel.CONNECT_ONLY
    probe_notes: list[str] = field(default_factory=list)
    probe_duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "responsive": self.responsive,
            "error": self.error,
            "supports_offline": self.supports_offline,
            "supports_online": self.supports_online,
            "supports_2pass": self.supports_2pass,
            "has_timestamp": self.has_timestamp,
            "has_stamp_sents": self.has_stamp_sents,
            "is_final_semantics": self.is_final_semantics,
            "inferred_server_type": self.inferred_server_type,
            "probe_level": self.probe_level.name,
            "probe_notes": self.probe_notes,
            "probe_duration_ms": self.probe_duration_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerCapabilities:
        level_str = data.get("probe_level", "CONNECT_ONLY")
        try:
            probe_level = ProbeLevel[level_str]
        except KeyError:
            probe_level = ProbeLevel.CONNECT_ONLY
        return cls(
            reachable=data.get("reachable", False),
            responsive=data.get("responsive", False),
            error=data.get("error"),
            supports_offline=data.get("supports_offline"),
            supports_online=data.get("supports_online"),
            supports_2pass=data.get("supports_2pass"),
            has_timestamp=data.get("has_timestamp", False),
            has_stamp_sents=data.get("has_stamp_sents", False),
            is_final_semantics=data.get("is_final_semantics", "unknown"),
            inferred_server_type=data.get("inferred_server_type", "unknown"),
            probe_level=probe_level,
            probe_notes=data.get("probe_notes", []),
            probe_duration_ms=data.get("probe_duration_ms", 0.0),
        )


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off", ""):
            return False
        return True
    return bool(value)


async def probe_server(
    host: str,
    port: int,
    *,
    use_ssl: bool = True,
    level: ProbeLevel = ProbeLevel.OFFLINE_LIGHT,
    timeout: float = 8.0,
) -> ServerCapabilities:
    """Probe a FunASR server's capabilities."""
    start = time.time()
    caps = ServerCapabilities(probe_level=level)

    if level == ProbeLevel.TWOPASS_FULL and timeout < 12.0:
        timeout = 12.0

    scheme = "wss" if use_ssl else "ws"
    uri = f"{scheme}://{host}:{port}"
    logger.info("server_probe_start", uri=uri, level=level.name)

    ssl_ctx: ssl.SSLContext | None = None
    if use_ssl:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        async with asyncio.timeout(timeout):
            async with connect_websocket(
                uri,
                subprotocols=["binary"],
                ping_interval=None,
                ssl=ssl_ctx,
                close_timeout=5,
            ) as ws:
                caps.reachable = True
                caps.probe_notes.append("WebSocket connected")

                if level == ProbeLevel.CONNECT_ONLY:
                    caps.probe_duration_ms = (time.time() - start) * 1000
                    logger.info("server_probe_done", uri=uri,
                                duration_ms=f"{caps.probe_duration_ms:.0f}",
                                reachable=True, level="CONNECT_ONLY")
                    return caps

                await _probe_offline(ws, caps)

                if level == ProbeLevel.TWOPASS_FULL and caps.reachable:
                    await _probe_2pass_new_conn(uri, ssl_ctx, caps, timeout)

    except asyncio.TimeoutError:
        caps.error = "probe timeout" if caps.reachable else "connection timeout"
    except ConnectionRefusedError:
        caps.error = "connection refused"
    except OSError as e:
        caps.error = f"network error: {e}"
    except Exception as e:
        exc_name = type(e).__name__
        if "ConnectionClosed" in exc_name:
            caps.reachable = True
            caps.error = f"connection closed: {e}"
        else:
            caps.error = str(e)
        logger.warning("server_probe_error", uri=uri, exc_type=exc_name, error=str(e))

    _infer_server_type(caps)
    caps.probe_duration_ms = (time.time() - start) * 1000
    logger.info("server_probe_done", uri=uri, duration_ms=f"{caps.probe_duration_ms:.0f}",
                reachable=caps.reachable, responsive=caps.responsive,
                inferred_type=caps.inferred_server_type)
    return caps


async def _probe_offline(ws: Any, caps: ServerCapabilities) -> None:
    """Send a short silence PCM clip in offline mode to probe capabilities."""
    try:
        probe_msg = json.dumps({
            "mode": "offline",
            "wav_name": "__probe__",
            "wav_format": "pcm",
            "audio_fs": 16000,
            "is_speaking": True,
            "itn": True,
        })
        await ws.send(probe_msg)

        silence_data = bytes(8000)  # 0.25s of 16kHz 16-bit mono silence
        await ws.send(silence_data)

        await ws.send(json.dumps({"is_speaking": False}))

        try:
            response = await asyncio.wait_for(ws.recv(), timeout=3.0)
            caps.responsive = True

            data = json.loads(response)
            caps.supports_offline = True

            if "timestamp" in data:
                caps.has_timestamp = True
            if "stamp_sents" in data:
                caps.has_stamp_sents = True

            raw_is_final = data.get("is_final")
            is_final = _coerce_bool(raw_is_final)
            if is_final is True:
                caps.is_final_semantics = "legacy_true"
            elif is_final is False:
                caps.is_final_semantics = "always_false"

            caps.probe_notes.append("offline probe OK")

        except asyncio.TimeoutError:
            caps.responsive = False
            caps.supports_offline = None
            caps.probe_notes.append("offline probe: no response (silent input may not produce output)")

    except Exception as e:
        caps.probe_notes.append(f"offline probe error: {e}")
        logger.warning("probe_offline_error", error=str(e))


async def _probe_2pass_new_conn(
    uri: str,
    ssl_ctx: ssl.SSLContext | None,
    caps: ServerCapabilities,
    timeout: float,
) -> None:
    """Probe 2pass mode using a fresh connection to avoid state interference."""
    try:
        async with asyncio.timeout(timeout):
            async with connect_websocket(
                uri,
                subprotocols=["binary"],
                ping_interval=None,
                ssl=ssl_ctx,
                close_timeout=5,
            ) as ws:
                probe_msg = json.dumps({
                    "mode": "2pass",
                    "wav_name": "__probe_2pass__",
                    "wav_format": "pcm",
                    "audio_fs": 16000,
                    "is_speaking": True,
                    "chunk_size": [5, 10, 5],
                    "chunk_interval": 10,
                })
                await ws.send(probe_msg)
                await ws.send(bytes(32000))  # 1s silence
                await ws.send(json.dumps({"is_speaking": False}))

                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(response)
                    mode = data.get("mode", "")
                    if mode in ("2pass", "2pass-online", "2pass-offline"):
                        caps.supports_2pass = True
                        caps.supports_online = True
                        caps.responsive = True
                        caps.probe_notes.append("2pass probe OK")
                except asyncio.TimeoutError:
                    caps.probe_notes.append("2pass probe: timeout")

    except Exception as e:
        caps.probe_notes.append(f"2pass probe connection error: {e}")
        logger.warning("probe_2pass_error", error=str(e))


def _infer_server_type(caps: ServerCapabilities) -> None:
    if caps.is_final_semantics == "always_false":
        caps.inferred_server_type = "funasr_main"
    elif caps.is_final_semantics == "legacy_true":
        caps.inferred_server_type = "legacy"
    else:
        caps.inferred_server_type = "unknown"
