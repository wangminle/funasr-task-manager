"""Audio preprocessing: convert, probe, VAD silence detection, and segment splitting."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

WAV_EXTENSIONS = {".wav", ".pcm"}
FFMPEG_BIN: str | None = None
FFPROBE_BIN: str | None = None
_MAX_CONVERSION_LOCKS = 500
_conversion_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _get_path_lock(key: str) -> asyncio.Lock:
    async with _locks_guard:
        if key not in _conversion_locks:
            if len(_conversion_locks) >= _MAX_CONVERSION_LOCKS:
                stale_keys = [k for k, v in _conversion_locks.items() if not v.locked()]
                for k in stale_keys[: len(_conversion_locks) - _MAX_CONVERSION_LOCKS + 1]:
                    del _conversion_locks[k]
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


def _find_ffprobe() -> str | None:
    global FFPROBE_BIN
    if FFPROBE_BIN is not None:
        return FFPROBE_BIN

    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        candidate = Path(ffmpeg).parent / "ffprobe"
        if candidate.exists():
            FFPROBE_BIN = str(candidate)
            return FFPROBE_BIN

    path = shutil.which("ffprobe")
    if path:
        FFPROBE_BIN = path
        return FFPROBE_BIN

    logger.warning("ffprobe_not_found")
    return None


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

        fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(settings.temp_dir))
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


# ---------------------------------------------------------------------------
# Audio probing
# ---------------------------------------------------------------------------

async def get_audio_duration_ms(audio_path: str) -> int:
    """Get audio duration in milliseconds using ffprobe."""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        raise RuntimeError("ffprobe not found on PATH; required for audio duration detection")

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed (exit {proc.returncode}): {stderr.decode(errors='replace')[:500]}")

    try:
        duration_sec = float(stdout.decode().strip())
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned non-numeric duration: {stdout.decode()[:200]}") from exc

    return int(duration_sec * 1000)


async def _probe_audio_format(audio_path: str) -> dict:
    """Probe first audio stream via ffprobe → {codec_name, sample_rate, channels}."""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        raise RuntimeError("ffprobe not found")

    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,codec_name",
        "-of", "csv=p=0",
        str(audio_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe format check failed: {stderr.decode(errors='replace')[:500]}")

    parts = stdout.decode().strip().split(",")
    if len(parts) < 3:
        raise RuntimeError(f"Unexpected ffprobe csv output: {stdout.decode()[:200]}")

    return {
        "codec_name": parts[0].strip(),
        "sample_rate": int(parts[1].strip()),
        "channels": int(parts[2].strip()),
    }


def _is_canonical_wav(info: dict) -> bool:
    """True when audio is already 16 kHz / mono / pcm_s16le."""
    return (
        info.get("codec_name") == "pcm_s16le"
        and info.get("sample_rate") == 16000
        and info.get("channels") == 1
    )


def _canonical_output_path(original_path: str) -> Path:
    src = Path(original_path)
    return src.parent / f"{src.stem}_canonical.wav"


async def ensure_canonical_wav(audio_path: str) -> str:
    """Guarantee 16 kHz mono s16 WAV output, converting if necessary.

    Unlike ``ensure_wav()`` which skips .wav files, this validates the actual
    stream format and re-encodes when the file is not canonical.  Required by
    the VAD segmentation pipeline for format consistency.
    """
    src = Path(audio_path)
    if not src.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    try:
        info = await _probe_audio_format(audio_path)
        if _is_canonical_wav(info) and src.suffix.lower() == ".wav":
            logger.debug("already_canonical_wav", path=audio_path)
            return audio_path
    except RuntimeError:
        pass  # probe failed — will attempt conversion anyway

    out_path = _canonical_output_path(audio_path)
    lock = await _get_path_lock(f"canonical:{out_path}")

    async with lock:
        if out_path.exists() and out_path.stat().st_size > 0:
            logger.debug("canonical_wav_cached", path=str(out_path))
            return str(out_path)

        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found; cannot produce canonical WAV")

        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(settings.temp_dir))
        os.close(fd)

        try:
            cmd = [
                ffmpeg, "-y",
                "-i", str(audio_path),
                "-ar", "16000",
                "-ac", "1",
                "-sample_fmt", "s16",
                "-f", "wav",
                tmp_path,
            ]
            logger.info("canonical_wav_converting", src=audio_path, dst=str(out_path))

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=1200)

            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg canonical conversion failed (exit {proc.returncode}): "
                    f"{stderr.decode(errors='replace')[-500:]}"
                )
            if Path(tmp_path).stat().st_size == 0:
                raise RuntimeError("ffmpeg produced empty canonical WAV output")

            os.replace(tmp_path, str(out_path))
            logger.info("canonical_wav_ok", src=audio_path, dst=str(out_path))
            return str(out_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Silence detection (VAD via ffmpeg silencedetect)
# ---------------------------------------------------------------------------

@dataclass
class SilenceRange:
    """A silence interval detected by ffmpeg, in milliseconds."""
    start_ms: int
    end_ms: int


_SILENCE_START_RE = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([\d.]+)")


def _parse_silencedetect_output(stderr_text: str) -> list[SilenceRange]:
    """Parse ``silence_start`` / ``silence_end`` lines from ffmpeg stderr."""
    ranges: list[SilenceRange] = []
    current_start: float | None = None

    for line in stderr_text.split("\n"):
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            current_start = float(m_start.group(1))
            continue

        m_end = _SILENCE_END_RE.search(line)
        if m_end and current_start is not None:
            end_sec = float(m_end.group(1))
            ranges.append(SilenceRange(
                start_ms=int(current_start * 1000),
                end_ms=int(end_sec * 1000),
            ))
            current_start = None

    return ranges


async def silence_detect(
    wav_path: str,
    *,
    noise_db: int | None = None,
    min_duration: float | None = None,
) -> list[SilenceRange]:
    """Detect silence ranges in a WAV file using ffmpeg silencedetect filter.

    Returns a sorted list of :class:`SilenceRange` (milliseconds).
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; required for silence detection")

    noise = noise_db if noise_db is not None else settings.segment_silence_noise_db
    dur = min_duration if min_duration is not None else settings.segment_silence_min_duration

    af_filter = f"silencedetect=n={noise}dB:d={dur}"
    cmd = [ffmpeg, "-i", str(wav_path), "-af", af_filter, "-f", "null", "-"]

    logger.info("silence_detect_start", path=wav_path, noise=f"{noise}dB", duration=dur)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    stderr_text = stderr.decode(errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(f"silencedetect failed (exit {proc.returncode}): {stderr_text[-500:]}")

    ranges = _parse_silencedetect_output(stderr_text)
    logger.info("silence_detect_done", path=wav_path, ranges_found=len(ranges))
    return ranges


# ---------------------------------------------------------------------------
# Segment planning (pure algorithm – no I/O)
# ---------------------------------------------------------------------------

@dataclass
class SegmentPlan:
    """Describes one segment for physical WAV splitting."""
    segment_index: int
    source_start_ms: int   # actual cut start (includes overlap)
    source_end_ms: int     # actual cut end (includes overlap)
    keep_start_ms: int     # logical start for result merge (no overlap)
    keep_end_ms: int       # logical end for result merge (no overlap)


def _find_best_cut(
    pos: int,
    silence_ranges: list[SilenceRange],
    *,
    target: int,
    step: int,
    rounds: int,
    primary_silence: int,
    fallback_silence: int,
    maximum: int,
) -> int:
    """Find the best cut point using progressive search with fallback.

    Returns the midpoint of the chosen silence range, or ``pos + maximum``
    as the hard-cut fallback.
    """
    def _midpoint(sr: SilenceRange) -> int:
        return (sr.start_ms + sr.end_ms) // 2

    def _duration(sr: SilenceRange) -> int:
        return sr.end_ms - sr.start_ms

    for r in range(rounds):
        lo = pos + target + r * step
        hi = lo + step
        candidates = [
            sr for sr in silence_ranges
            if lo <= _midpoint(sr) <= hi and _duration(sr) >= primary_silence
        ]
        if candidates:
            best = max(candidates, key=_duration)
            return _midpoint(best)

    full_lo = pos + target
    full_hi = pos + maximum
    fallback_candidates = [
        sr for sr in silence_ranges
        if full_lo <= _midpoint(sr) <= full_hi and _duration(sr) >= fallback_silence
    ]
    if fallback_candidates:
        best = max(fallback_candidates, key=_duration)
        return _midpoint(best)

    return pos + maximum


def plan_segments(
    total_duration_ms: int,
    silence_ranges: list[SilenceRange],
    *,
    target_duration_ms: int | None = None,
    min_duration_ms: int | None = None,
    max_duration_ms: int | None = None,
    overlap_ms: int | None = None,
    search_step_ms: int | None = None,
    search_max_rounds: int | None = None,
    fallback_silence_ms: int | None = None,
    min_silence_ms: int | None = None,
) -> list[SegmentPlan]:
    """Generate a split plan for long audio using progressive search.

    Cut-point selection uses a multi-round progressive strategy:

    1. **Round 1..N**: Each round searches a ``search_step_ms``-wide window
       starting from ``target_duration_ms`` onward.  Within each window only
       silences ≥ ``min_silence_ms`` are considered; the longest wins.
    2. **Fallback round**: If all rounds fail, the entire range
       ``[target, max]`` is searched with a lowered threshold
       (``fallback_silence_ms``, e.g. 300 ms for sentence-gap pauses).
    3. **Hard cut**: As a last resort, cut at ``max_duration_ms``.

    A trailing fragment shorter than ``min_duration_ms`` is merged with the
    preceding segment.  Returns a single-element list when the audio fits
    within ``max_duration_ms``.
    """
    target = (target_duration_ms if target_duration_ms is not None
              else settings.segment_target_duration_sec * 1000)
    minimum = (min_duration_ms if min_duration_ms is not None
               else settings.segment_min_duration_sec * 1000)
    maximum = (max_duration_ms if max_duration_ms is not None
               else settings.segment_max_duration_sec * 1000)
    overlap = overlap_ms if overlap_ms is not None else settings.segment_overlap_ms
    step = (search_step_ms if search_step_ms is not None
            else settings.segment_search_step_sec * 1000)
    rounds = (search_max_rounds if search_max_rounds is not None
              else settings.segment_search_max_rounds)
    fallback_silence = (fallback_silence_ms if fallback_silence_ms is not None
                        else int(settings.segment_fallback_silence_sec * 1000))
    primary_silence = (min_silence_ms if min_silence_ms is not None
                       else int(settings.segment_silence_min_duration * 1000))

    if total_duration_ms <= maximum:
        return [SegmentPlan(
            segment_index=0,
            source_start_ms=0,
            source_end_ms=total_duration_ms,
            keep_start_ms=0,
            keep_end_ms=total_duration_ms,
        )]

    cut_points: list[int] = []
    pos = 0

    while True:
        remaining = total_duration_ms - pos
        if remaining <= maximum:
            break

        cut = _find_best_cut(
            pos, silence_ranges,
            target=target, step=step, rounds=rounds,
            primary_silence=primary_silence,
            fallback_silence=fallback_silence,
            maximum=maximum,
        )

        if total_duration_ms - cut < minimum:
            break

        cut_points.append(cut)
        pos = cut

    boundaries = [0] + cut_points + [total_duration_ms]
    plans: list[SegmentPlan] = []
    last_idx = len(boundaries) - 2

    for i in range(len(boundaries) - 1):
        keep_start = boundaries[i]
        keep_end = boundaries[i + 1]

        source_start = max(0, keep_start - overlap) if i > 0 else 0
        source_end = (min(total_duration_ms, keep_end + overlap)
                      if i < last_idx else total_duration_ms)

        plans.append(SegmentPlan(
            segment_index=i,
            source_start_ms=source_start,
            source_end_ms=source_end,
            keep_start_ms=keep_start,
            keep_end_ms=keep_end,
        ))

    return plans


# ---------------------------------------------------------------------------
# Physical WAV splitting
# ---------------------------------------------------------------------------

async def split_wav_segments(
    wav_path: str,
    plans: list[SegmentPlan],
    output_dir: str,
    task_id: str,
) -> list[str]:
    """Split a canonical WAV into per-segment files.

    Uses atomic write (tmp + rename) per segment.  Existing segment files
    are re-used (idempotent).  Returns paths in ``segment_index`` order.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; cannot split WAV segments")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    output_paths: list[str] = []

    for plan in sorted(plans, key=lambda p: p.segment_index):
        final_path = out_dir / f"{task_id}_seg{plan.segment_index:03d}.wav"

        if final_path.exists() and final_path.stat().st_size > 0:
            output_paths.append(str(final_path))
            continue

        start_sec = plan.source_start_ms / 1000.0
        duration_sec = (plan.source_end_ms - plan.source_start_ms) / 1000.0

        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(settings.temp_dir))
        os.close(fd)

        try:
            cmd = [
                ffmpeg, "-y",
                "-i", str(wav_path),
                "-ss", f"{start_sec:.3f}",
                "-t", f"{duration_sec:.3f}",
                "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
                "-f", "wav",
                tmp_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

            if proc.returncode != 0:
                raise RuntimeError(
                    f"Segment split failed (idx={plan.segment_index}): "
                    f"{stderr.decode(errors='replace')[-300:]}"
                )
            if Path(tmp_path).stat().st_size == 0:
                raise RuntimeError(
                    f"ffmpeg produced empty output for segment {plan.segment_index}"
                )

            os.replace(tmp_path, str(final_path))
            output_paths.append(str(final_path))

            logger.info(
                "segment_split_ok",
                task_id=task_id,
                idx=plan.segment_index,
                start_ms=plan.source_start_ms,
                end_ms=plan.source_end_ms,
                path=str(final_path),
            )
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    return output_paths
