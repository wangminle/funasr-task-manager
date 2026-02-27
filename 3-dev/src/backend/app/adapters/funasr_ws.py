"""FunASR WebSocket protocol adapter. Handles both new (FunASR-main) and legacy server versions."""

import asyncio
import json
from pathlib import Path

import websockets

from app.adapters.base import BaseAdapter, MessageProfile, ParsedResult, RecognitionMode, ServerType
from app.observability.logging import get_logger

logger = get_logger(__name__)
CHUNK_SIZE = 16000


class FunASRWebSocketAdapter(BaseAdapter):
    def __init__(self, server_type: ServerType = ServerType.AUTO):
        self._server_type = server_type

    @property
    def server_type(self) -> ServerType:
        return self._server_type

    @server_type.setter
    def server_type(self, value: ServerType) -> None:
        self._server_type = value

    def build_start_message(self, profile: MessageProfile) -> str:
        msg: dict = {
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
    def _coerce_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return False

    async def transcribe(self, host: str, port: int, audio_path: str, profile: MessageProfile, *, use_ssl: bool = True, timeout: float = 300.0) -> ParsedResult:
        scheme = "wss" if use_ssl else "ws"
        uri = f"{scheme}://{host}:{port}"
        logger.info("transcription_starting", uri=uri, audio=audio_path, mode=profile.mode.value)
        try:
            async with asyncio.timeout(timeout):
                async with websockets.connect(uri, max_size=None, close_timeout=5) as ws:
                    await ws.send(self.build_start_message(profile))
                    audio_data = Path(audio_path).read_bytes()
                    for offset in range(0, len(audio_data), CHUNK_SIZE):
                        chunk = audio_data[offset : offset + CHUNK_SIZE]
                        await ws.send(chunk)
                    await ws.send(self.build_end_message())
                    final_result = ParsedResult()
                    async for msg in ws:
                        if isinstance(msg, bytes):
                            continue
                        result = self.parse_result(msg)
                        if result.error:
                            logger.warning("parse_error", error=result.error)
                            continue
                        if result.text:
                            final_result = result
                        if result.is_complete:
                            final_result = result
                            break
                    logger.info("transcription_completed", text_length=len(final_result.text), is_complete=final_result.is_complete)
                    return final_result
        except TimeoutError:
            logger.error("transcription_timeout", timeout=timeout, audio=audio_path)
            return ParsedResult(error=f"Transcription timed out after {timeout}s")
        except websockets.exceptions.WebSocketException as e:
            logger.error("websocket_error", error=str(e), uri=uri)
            return ParsedResult(error=f"WebSocket error: {e}")
        except Exception as e:
            logger.error("transcription_error", error=str(e), uri=uri)
            return ParsedResult(error=f"Unexpected error: {e}")
