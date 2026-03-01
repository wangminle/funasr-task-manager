"""FunASR WebSocket protocol adapter.

Handles both new (FunASR-main) and legacy server versions.
Ported and aligned with funasr-client-python's protocol_adapter + simple_funasr_client.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import wave
from pathlib import Path
from typing import Any

from app.adapters.base import BaseAdapter, MessageProfile, ParsedResult, RecognitionMode, ServerType
from app.adapters.websocket_compat import connect_websocket
from app.observability.logging import get_logger

logger = get_logger(__name__)

OFFLINE_CHUNK_SIZE = 65536
STREAMING_CHUNK_FACTOR = 60


def _make_no_verify_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def read_audio_file(audio_path: str, default_sample_rate: int = 16000) -> tuple[bytes | None, int, str]:
    """Read audio file, extracting raw PCM from WAV or returning raw bytes for other formats.

    Aligned with funasr-client-python's read_audio_file logic.

    Returns:
        (audio_bytes, sample_rate, wav_format) where wav_format is "pcm" or "others".
    """
    ext = Path(audio_path).suffix.lower()

    try:
        if ext == ".pcm":
            with open(audio_path, "rb") as f:
                return f.read(), default_sample_rate, "pcm"

        if ext == ".wav":
            try:
                with wave.open(audio_path, "rb") as wav_file:
                    sample_rate = wav_file.getframerate()
                    frames = wav_file.readframes(wav_file.getnframes())
                    logger.debug("wav_file_read", path=audio_path, sample_rate=sample_rate,
                                 frames=wav_file.getnframes(), channels=wav_file.getnchannels())
                    return bytes(frames), sample_rate, "pcm"
            except wave.Error:
                logger.warning("wav_header_invalid_fallback_others", path=audio_path)
                with open(audio_path, "rb") as f:
                    return f.read(), default_sample_rate, "others"

        with open(audio_path, "rb") as f:
            return f.read(), default_sample_rate, "others"

    except Exception as e:
        logger.error("audio_file_read_error", path=audio_path, error=str(e))
        return None, default_sample_rate, "others"


class FunASRWebSocketAdapter(BaseAdapter):
    """WebSocket adapter for FunASR servers (legacy + main)."""

    def __init__(self, server_type: ServerType = ServerType.AUTO):
        self._server_type = server_type
        self._detected_is_final_semantics = "unknown"

    @property
    def server_type(self) -> ServerType:
        return self._server_type

    @server_type.setter
    def server_type(self, value: ServerType) -> None:
        self._server_type = value

    def build_start_message(self, profile: MessageProfile) -> str:
        msg: dict[str, Any] = {
            "mode": profile.mode.value,
            "wav_name": profile.wav_name,
            "wav_format": profile.wav_format,
            "audio_fs": profile.audio_fs,
            "is_speaking": True,
            "itn": profile.use_itn,
        }
        if profile.hotwords:
            msg["hotwords"] = profile.hotwords

        if profile.mode in (RecognitionMode.ONLINE, RecognitionMode.TWOPASS):
            msg["chunk_size"] = profile.chunk_size
            msg["chunk_interval"] = profile.chunk_interval

        effective_type = profile.server_type if profile.server_type != ServerType.AUTO else self._server_type
        if profile.enable_svs_params or effective_type == ServerType.FUNASR_MAIN:
            msg["svs_lang"] = profile.svs_lang
            msg["svs_itn"] = profile.svs_itn

        return json.dumps(msg, ensure_ascii=False)

    def build_end_message(self) -> str:
        return json.dumps({"is_speaking": False})

    def parse_result(self, raw_msg: str) -> ParsedResult:
        result = ParsedResult(raw_string=raw_msg)
        try:
            data = json.loads(raw_msg)
        except (json.JSONDecodeError, TypeError) as e:
            result.error = f"JSON parse error: {e}"
            return result
        result.raw = data
        result.mode = data.get("mode", "")
        result.wav_name = data.get("wav_name", "")
        result.is_final = self._coerce_bool(data.get("is_final", False))
        result.timestamp = data.get("timestamp")
        result.stamp_sents = data.get("stamp_sents")
        result.text = self._extract_text(data)
        result.is_complete = self._should_complete(data)

        if result.mode == "offline":
            self._record_is_final_semantics(result.is_final)

        return result

    def _extract_text(self, data: dict) -> str:
        text = data.get("text", "")
        if text:
            return str(text)
        stamp_sents = data.get("stamp_sents")
        if stamp_sents and isinstance(stamp_sents, list):
            segments = [s.get("text_seg", "") for s in stamp_sents if isinstance(s, dict)]
            joined = "".join(segments)
            if joined:
                return joined
        for fallback in ("text_2pass_offline", "text_2pass_online"):
            val = data.get(fallback, "")
            if val:
                return str(val)
        return ""

    def _should_complete(self, data: dict) -> bool:
        """Determine whether the recognition session is complete.

        Core fix from funasr-client-python: offline mode completes on any response
        (not dependent on is_final), 2pass completes on 2pass-offline.
        """
        is_final = self._coerce_bool(data.get("is_final", False))
        if is_final:
            return True

        mode = data.get("mode", "").lower()
        if mode == "offline":
            return True
        if "2pass-offline" in mode or "2pass_offline" in mode:
            return True

        stamp_sents = data.get("stamp_sents")
        if stamp_sents and isinstance(stamp_sents, list) and len(stamp_sents) > 0:
            return True

        return False

    @staticmethod
    def _coerce_bool(value: Any) -> bool:
        """Tolerant bool coercion matching funasr-client-python's logic."""
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "y", "on")
        return bool(value)

    def _record_is_final_semantics(self, is_final: bool) -> None:
        if is_final:
            self._detected_is_final_semantics = "legacy_true"
        else:
            self._detected_is_final_semantics = "always_false"

    async def transcribe(
        self,
        host: str,
        port: int,
        audio_path: str,
        profile: MessageProfile,
        *,
        use_ssl: bool = True,
        ssl_verify: bool = False,
        timeout: float = 300.0,
    ) -> ParsedResult:
        scheme = "wss" if use_ssl else "ws"
        uri = f"{scheme}://{host}:{port}"
        logger.info("transcription_starting", uri=uri, audio=audio_path, mode=profile.mode.value)

        audio_bytes, detected_sample_rate, detected_wav_format = read_audio_file(audio_path, profile.audio_fs)
        if audio_bytes is None:
            return ParsedResult(error=f"Failed to read audio file: {audio_path}")

        if profile.wav_format == "pcm" and detected_wav_format != "pcm":
            profile.wav_format = detected_wav_format
        if detected_sample_rate != profile.audio_fs and detected_wav_format == "pcm":
            profile.audio_fs = detected_sample_rate

        ssl_ctx: ssl.SSLContext | None = None
        if use_ssl:
            ssl_ctx = ssl.create_default_context() if ssl_verify else _make_no_verify_ssl_context()

        if profile.mode == RecognitionMode.OFFLINE:
            stride = OFFLINE_CHUNK_SIZE
        else:
            stride = int(STREAMING_CHUNK_FACTOR * profile.chunk_size[1] / profile.chunk_interval / 1000 * profile.audio_fs * 2)
            stride = max(stride, 4096)

        chunk_num = max(1, (len(audio_bytes) - 1) // stride + 1)
        audio_size_mb = len(audio_bytes) / (1024 * 1024)
        logger.info("audio_prepared", size_mb=f"{audio_size_mb:.2f}",
                     chunks=chunk_num, stride=stride, wav_format=profile.wav_format,
                     sample_rate=profile.audio_fs)

        start_time = time.time()

        try:
            async with asyncio.timeout(timeout):
                async with connect_websocket(
                    uri,
                    subprotocols=["binary"],
                    ping_interval=None,
                    ssl=ssl_ctx,
                    close_timeout=60,
                    max_size=1024 * 1024 * 1024,
                ) as ws:
                    start_msg = self.build_start_message(profile)
                    await ws.send(start_msg)

                    for i in range(chunk_num):
                        beg = i * stride
                        end = min(beg + stride, len(audio_bytes))
                        await ws.send(audio_bytes[beg:end])

                    await ws.send(self.build_end_message())

                    upload_time = time.time() - start_time
                    logger.info("audio_upload_complete", upload_time=f"{upload_time:.2f}s",
                                speed_mbps=f"{audio_size_mb / max(upload_time, 0.001):.2f}")

                    final_result = ParsedResult()
                    msg_count = 0
                    text_msg_count = 0
                    total_text_len = 0
                    recv_start = time.time()

                    async for raw_msg in ws:
                        if isinstance(raw_msg, bytes):
                            continue

                        msg_count += 1
                        result = self.parse_result(raw_msg)

                        if result.error:
                            logger.warning("parse_error", error=result.error, msg_num=msg_count)
                            continue

                        text_msg_count += 1
                        if result.text:
                            total_text_len += len(result.text)
                            final_result = result

                        if result.is_complete:
                            final_result = result
                            break

                    recv_time = time.time() - recv_start
                    total_time = time.time() - start_time

                    if text_msg_count == 0:
                        final_result = ParsedResult(
                            error="No valid response received from server (connection closed without messages)"
                        )

                    logger.info("transcription_completed",
                                text_length=len(final_result.text),
                                total_text_length=total_text_len,
                                messages=msg_count,
                                text_messages=text_msg_count,
                                is_complete=final_result.is_complete,
                                has_error=final_result.error is not None,
                                upload_time=f"{upload_time:.2f}s",
                                recv_time=f"{recv_time:.2f}s",
                                total_time=f"{total_time:.2f}s")
                    return final_result

        except TimeoutError:
            logger.error("transcription_timeout", timeout=timeout, audio=audio_path)
            return ParsedResult(error=f"Transcription timed out after {timeout}s")
        except Exception as e:
            exc_name = type(e).__name__
            if "WebSocket" in exc_name or "ConnectionClosed" in exc_name:
                logger.error("websocket_error", error=str(e), uri=uri, exc_type=exc_name)
                return ParsedResult(error=f"WebSocket error: {e}")
            logger.error("transcription_error", error=str(e), uri=uri, exc_type=exc_name)
            return ParsedResult(error=f"Unexpected error ({exc_name}): {e}")
