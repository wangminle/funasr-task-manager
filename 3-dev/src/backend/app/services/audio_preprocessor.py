"""Audio preprocessing: convert non-WAV files to 16kHz mono PCM WAV for FunASR."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

WAV_EXTENSIONS = {".wav", ".pcm"}
FFMPEG_BIN: str | None = None
_conversion_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_path_lock(key: str) -> asyncio.Lock:
    async with _locks_guard:
        if key not in _conversion_locks:
            _conversion_locks[key] = asyncio.Lock()
        return _conversion_locks[key]


def _find_ffmpeg() -> str | None:
    global FFMPEG_BIN
    if FFMPEG_BIN is not None:
        return FFMPEG_BIN

    path = shutil.which("ffmpeg")
    if path:
        FFMPEG_BIN = path
        logger.info("ffmpeg_found", path=path, source="PATH")
        return FFMPEG_BIN

    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).exists():
            FFMPEG_BIN = path
            logger.info("ffmpeg_found", path=path, source="imageio_ffmpeg")
            return FFMPEG_BIN
    except ImportError:
        pass

    logger.warning("ffmpeg_not_found")
    return FFMPEG_BIN


def needs_conversion(audio_path: str) -> bool:
    ext = Path(audio_path).suffix.lower()
    return ext not in WAV_EXTENSIONS


def _wav_output_path(original_path: str) -> Path:
    """Generate a converted WAV path alongside the original file."""
    src = Path(original_path)
    return src.parent / f"{src.stem}_converted.wav"


async def ensure_wav(audio_path: str) -> str:
    """Return a WAV path suitable for FunASR.

    If the file is already WAV, return the original path.
    Otherwise, convert it using ffmpeg and return the new path.
    Uses per-file locking + atomic rename to prevent concurrent tasks
    from reading a half-written conversion output.
    Raises RuntimeError if conversion fails or ffmpeg is unavailable.
    """
    if not needs_conversion(audio_path):
        return audio_path

    out_path = _wav_output_path(audio_path)
    lock = await _get_path_lock(str(out_path))

    async with lock:
        if out_path.exists() and out_path.stat().st_size > 0:
            logger.debug("wav_already_converted", path=str(out_path))
            return str(out_path)

        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError(
                "ffmpeg not found on PATH. Install ffmpeg to enable audio format conversion. "
                "Non-WAV files cannot be processed without ffmpeg."
            )

        settings.temp_dir.mkdir(parents=True, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(out_path.parent))
        os.close(fd)

        try:
            cmd = [
                ffmpeg,
                "-y",
                "-i", str(audio_path),
                "-ar", "16000",
                "-ac", "1",
                "-sample_fmt", "s16",
                "-f", "wav",
                tmp_path,
            ]

            logger.info("ffmpeg_converting", src=audio_path, dst=str(out_path), tmp=tmp_path)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

            if proc.returncode != 0:
                err_msg = stderr.decode(errors="replace")[-500:]
                logger.error("ffmpeg_conversion_failed", src=audio_path, returncode=proc.returncode, stderr=err_msg)
                raise RuntimeError(f"ffmpeg conversion failed (exit {proc.returncode}): {err_msg}")

            tmp_stat = Path(tmp_path).stat()
            if tmp_stat.st_size == 0:
                raise RuntimeError(f"ffmpeg produced empty output: {tmp_path}")

            os.replace(tmp_path, str(out_path))
            logger.info("ffmpeg_conversion_ok", src=audio_path, dst=str(out_path), size=tmp_stat.st_size)
            return str(out_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
