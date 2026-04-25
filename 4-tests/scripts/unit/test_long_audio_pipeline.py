"""Integration tests for VAD segmentation pipeline using real long audio files.

Requires ffmpeg on PATH and audio files in:
  4-tests/batch-testing/assets/3-长音频/

Files:
  - 20240510_160113.m4a   (~38 MB)
  - 241002-GuruMoringTeaching.mp3  (~187 MB)
  - teslaFSD12.x-trial.mp4  (~432 MB)
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from app.services.audio_preprocessor import (
    SilenceRange,
    SegmentPlan,
    ensure_canonical_wav,
    get_audio_duration_ms,
    plan_segments,
    silence_detect,
    split_wav_segments,
)
from app.config import settings

ASSETS_DIR = Path(__file__).resolve().parents[2] / "batch-testing" / "assets" / "3-长音频"

AUDIO_FILES = [
    "20240510_160113.m4a",
    "241002-GuruMoringTeaching.mp3",
    "teslaFSD12.x-trial.mp4",
]

PLAN_DEFAULTS = dict(
    target_duration_ms=600_000,
    min_duration_ms=120_000,
    max_duration_ms=780_000,
    overlap_ms=400,
    search_step_ms=60_000,
    search_max_rounds=3,
    fallback_silence_ms=300,
    min_silence_ms=800,
)


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _have_audio_file(name: str) -> bool:
    return (ASSETS_DIR / name).exists()


skip_no_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not on PATH")


def _skip_if_missing(name: str):
    return pytest.mark.skipif(
        not _have_audio_file(name),
        reason=f"Audio file not found: {name}",
    )


@skip_no_ffmpeg
@pytest.mark.integration
class TestLongAudioPipeline:
    """Full pipeline tests: canonical WAV → silence detect → plan → validate."""

    @pytest.fixture(autouse=True)
    def _ensure_temp_dir(self, tmp_path):
        self.tmp_dir = tmp_path

    @_skip_if_missing("20240510_160113.m4a")
    @pytest.mark.asyncio
    async def test_m4a_segmentation(self):
        await self._run_pipeline("20240510_160113.m4a")

    @_skip_if_missing("241002-GuruMoringTeaching.mp3")
    @pytest.mark.asyncio
    async def test_mp3_segmentation(self):
        await self._run_pipeline("241002-GuruMoringTeaching.mp3")

    @_skip_if_missing("teslaFSD12.x-trial.mp4")
    @pytest.mark.asyncio
    async def test_mp4_segmentation(self):
        await self._run_pipeline("teslaFSD12.x-trial.mp4")

    async def _run_pipeline(self, filename: str) -> None:
        audio_path = str(ASSETS_DIR / filename)
        print(f"\n{'='*60}")
        print(f"Testing: {filename}")
        print(f"{'='*60}")

        # 1. Get original duration
        duration_ms = await get_audio_duration_ms(audio_path)
        duration_sec = duration_ms / 1000
        duration_min = duration_sec / 60
        print(f"Duration: {duration_sec:.1f}s ({duration_min:.1f} min)")

        # 2. Canonical WAV conversion
        canonical_path = await ensure_canonical_wav(audio_path)
        assert Path(canonical_path).exists()
        canonical_size = Path(canonical_path).stat().st_size
        print(f"Canonical WAV: {canonical_path}")
        print(f"Canonical size: {canonical_size / 1024 / 1024:.1f} MB")

        # 3. Silence detection
        silence_ranges = await silence_detect(canonical_path)
        print(f"Silence ranges found: {len(silence_ranges)}")
        if silence_ranges[:5]:
            for sr in silence_ranges[:5]:
                dur = sr.end_ms - sr.start_ms
                print(f"  [{sr.start_ms/1000:.1f}s - {sr.end_ms/1000:.1f}s] "
                      f"duration={dur}ms")
            if len(silence_ranges) > 5:
                print(f"  ... and {len(silence_ranges) - 5} more")

        # 4. Plan segments
        plans = plan_segments(duration_ms, silence_ranges, **PLAN_DEFAULTS)
        print(f"\nSegment plan ({len(plans)} segments):")
        for p in plans:
            keep_dur = (p.keep_end_ms - p.keep_start_ms) / 1000
            src_dur = (p.source_end_ms - p.source_start_ms) / 1000
            print(f"  Seg {p.segment_index}: "
                  f"keep=[{p.keep_start_ms/1000:.1f}s, {p.keep_end_ms/1000:.1f}s] "
                  f"({keep_dur:.1f}s) "
                  f"source=[{p.source_start_ms/1000:.1f}s, {p.source_end_ms/1000:.1f}s] "
                  f"({src_dur:.1f}s)")

        # 5. Validate plan
        self._validate_plan(plans, duration_ms)

        # 6. Validate search window compliance
        self._validate_search_window(plans, silence_ranges, duration_ms)

        # 7. Physical split (only if > 1 segment)
        if len(plans) > 1:
            output_dir = str(self.tmp_dir / "segments")
            task_id = f"test_{Path(filename).stem}"
            segment_paths = await split_wav_segments(
                canonical_path, plans, output_dir, task_id,
            )
            assert len(segment_paths) == len(plans)
            print(f"\nPhysical segment files:")
            for i, sp in enumerate(segment_paths):
                size_mb = Path(sp).stat().st_size / 1024 / 1024
                print(f"  Seg {i}: {Path(sp).name} ({size_mb:.1f} MB)")
                assert Path(sp).exists()
                assert Path(sp).stat().st_size > 0

        print(f"\n{'='*60}")
        print(f"PASS: {filename}")
        print(f"{'='*60}\n")

    def _validate_plan(self, plans: list[SegmentPlan], total_ms: int) -> None:
        assert len(plans) >= 1

        assert plans[0].keep_start_ms == 0
        assert plans[-1].keep_end_ms == total_ms

        for i in range(1, len(plans)):
            assert plans[i].keep_start_ms == plans[i - 1].keep_end_ms, (
                f"Gap between segment {i-1} and {i}"
            )

        assert plans[0].source_start_ms == 0
        assert plans[-1].source_end_ms == total_ms

        for i, p in enumerate(plans):
            assert p.source_start_ms <= p.keep_start_ms
            assert p.source_end_ms >= p.keep_end_ms
            assert p.keep_end_ms > p.keep_start_ms
            if i > 0:
                assert p.source_start_ms < p.keep_start_ms, (
                    f"Middle segment {i} should have leading overlap"
                )
            if i < len(plans) - 1:
                assert p.source_end_ms > p.keep_end_ms, (
                    f"Non-last segment {i} should have trailing overlap"
                )

    def _validate_search_window(
        self,
        plans: list[SegmentPlan],
        silence_ranges: list[SilenceRange],
        total_ms: int,
    ) -> None:
        """Verify each cut point falls within the progressive search range
        [target, max] = [600s, 780s] from segment start, or is a hard cut."""
        if len(plans) <= 1:
            return

        window_lo = 600_000
        window_hi = 780_000

        for i in range(len(plans) - 1):
            cut_ms = plans[i].keep_end_ms
            seg_start = plans[i].keep_start_ms
            offset = cut_ms - seg_start

            assert offset >= window_lo - 1, (
                f"Segment {i} cut at {offset}ms from start, "
                f"below window start {window_lo}ms"
            )
            assert offset <= window_hi + 1, (
                f"Segment {i} cut at {offset}ms from start, "
                f"above window end {window_hi}ms"
            )
