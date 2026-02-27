"""Audio/video metadata extraction using ffprobe."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass
class MediaMetadata:
    duration_sec: float | None = None
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    media_type: str | None = None
    mime: str | None = None
    error: str | None = None


async def extract_metadata(file_path: str | Path) -> MediaMetadata:
    path = Path(file_path)
    if not path.exists():
        return MediaMetadata(error=f"File not found: {path}")
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning("ffprobe_failed", file=str(path), error=err)
            return MediaMetadata(error=f"ffprobe error: {err}")
        data = json.loads(stdout.decode())
        return _parse_ffprobe_output(data)
    except FileNotFoundError:
        return MediaMetadata(error="ffprobe not found. Install ffmpeg.")
    except asyncio.TimeoutError:
        return MediaMetadata(error="ffprobe timed out")
    except Exception as e:
        logger.error("metadata_extraction_error", error=str(e), file=str(path))
        return MediaMetadata(error=str(e))


def _parse_ffprobe_output(data: dict) -> MediaMetadata:
    meta = MediaMetadata()
    fmt = data.get("format", {})
    meta.duration_sec = float(fmt.get("duration", 0)) or None
    streams = data.get("streams", [])
    audio_stream = None
    video_stream = None
    for s in streams:
        codec_type = s.get("codec_type", "")
        if codec_type == "audio" and audio_stream is None:
            audio_stream = s
        elif codec_type == "video" and video_stream is None:
            video_stream = s
    if video_stream and video_stream.get("codec_name") not in ("mjpeg", "png"):
        meta.media_type = "video"
        meta.codec = video_stream.get("codec_name")
    elif audio_stream:
        meta.media_type = "audio"
    else:
        meta.media_type = "unknown"
    if audio_stream:
        meta.codec = meta.codec or audio_stream.get("codec_name")
        sr = audio_stream.get("sample_rate")
        meta.sample_rate = int(sr) if sr else None
        ch = audio_stream.get("channels")
        meta.channels = int(ch) if ch else None
    fmt_name = fmt.get("format_name", "")
    mime_map = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg", "mp4": "video/mp4", "matroska": "video/x-matroska", "webm": "video/webm"}
    for key, mime in mime_map.items():
        if key in fmt_name:
            meta.mime = mime
            break
    return meta
