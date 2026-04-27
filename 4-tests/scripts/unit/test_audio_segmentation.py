"""Unit tests for audio segmentation: silencedetect parsing, plan algorithm, format check."""

import pytest

from app.services.audio_preprocessor import (
    SilenceRange,
    SegmentPlan,
    _is_canonical_wav,
    _parse_silencedetect_output,
    _find_best_cut,
    plan_segments,
)

# ---------------------------------------------------------------------------
# _is_canonical_wav
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestIsCanonicalWav:
    def test_canonical(self):
        assert _is_canonical_wav({"codec_name": "pcm_s16le", "sample_rate": 16000, "channels": 1})

    def test_wrong_sample_rate(self):
        assert not _is_canonical_wav({"codec_name": "pcm_s16le", "sample_rate": 44100, "channels": 1})

    def test_wrong_channels(self):
        assert not _is_canonical_wav({"codec_name": "pcm_s16le", "sample_rate": 16000, "channels": 2})

    def test_wrong_codec(self):
        assert not _is_canonical_wav({"codec_name": "mp3", "sample_rate": 16000, "channels": 1})

    def test_empty_dict(self):
        assert not _is_canonical_wav({})

    def test_pcm_s16be(self):
        assert not _is_canonical_wav({"codec_name": "pcm_s16be", "sample_rate": 16000, "channels": 1})


# ---------------------------------------------------------------------------
# _parse_silencedetect_output
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseSilencedetectOutput:
    SAMPLE_OUTPUT = (
        "size=N/A time=00:20:00.00 bitrate=N/A speed= 120x\n"
        "[silencedetect @ 0xdead] silence_start: 2.504\n"
        "[silencedetect @ 0xdead] silence_end: 3.201 | silence_duration: 0.697\n"
        "[silencedetect @ 0xdead] silence_start: 480.100\n"
        "[silencedetect @ 0xdead] silence_end: 481.500 | silence_duration: 1.400\n"
        "[silencedetect @ 0xdead] silence_start: 960.000\n"
        "[silencedetect @ 0xdead] silence_end: 961.200 | silence_duration: 1.200\n"
    )

    def test_normal_parse(self):
        ranges = _parse_silencedetect_output(self.SAMPLE_OUTPUT)
        assert len(ranges) == 3
        assert ranges[0] == SilenceRange(start_ms=2504, end_ms=3201)
        assert ranges[1] == SilenceRange(start_ms=480100, end_ms=481500)
        assert ranges[2] == SilenceRange(start_ms=960000, end_ms=961200)

    def test_empty_output(self):
        assert _parse_silencedetect_output("") == []
        assert _parse_silencedetect_output("random ffmpeg output\nno silence\n") == []

    def test_unmatched_start(self):
        """silence_start without a following silence_end is ignored."""
        text = (
            "[silencedetect @ 0xa] silence_start: 10.0\n"
            "[silencedetect @ 0xa] silence_end: 11.0 | silence_duration: 1.0\n"
            "[silencedetect @ 0xa] silence_start: 1199.5\n"
        )
        ranges = _parse_silencedetect_output(text)
        assert len(ranges) == 1
        assert ranges[0] == SilenceRange(start_ms=10000, end_ms=11000)

    def test_integer_values(self):
        text = (
            "[silencedetect @ 0xf] silence_start: 0\n"
            "[silencedetect @ 0xf] silence_end: 2 | silence_duration: 2\n"
        )
        ranges = _parse_silencedetect_output(text)
        assert len(ranges) == 1
        assert ranges[0] == SilenceRange(start_ms=0, end_ms=2000)

    def test_high_precision(self):
        text = (
            "[silencedetect @ 0xb] silence_start: 123.456789\n"
            "[silencedetect @ 0xb] silence_end: 124.987654 | silence_duration: 1.530865\n"
        )
        ranges = _parse_silencedetect_output(text)
        assert len(ranges) == 1
        assert ranges[0].start_ms == 123456
        assert ranges[0].end_ms == 124987


# ---------------------------------------------------------------------------
# _find_best_cut — progressive search algorithm
# ---------------------------------------------------------------------------

CUT_DEFAULTS = dict(
    target=600_000,
    step=60_000,
    rounds=3,
    primary_silence=800,
    fallback_silence=300,
    maximum=720_000,
)


@pytest.mark.unit
class TestFindBestCut:
    """Tests for the progressive cut-point search function."""

    def test_round1_hit(self):
        """Silence in round 1 window [600s, 660s] → pick it."""
        silence = [SilenceRange(start_ms=619_000, end_ms=621_000)]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 620_000

    def test_forward_round2_hit(self):
        """No round 0 (forward) or round 1 (backward), silence in round 2 (forward) [660s, 720s]."""
        silence = [SilenceRange(start_ms=699_000, end_ms=701_000)]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 700_000

    def test_backward_round_hit(self):
        """No silence forward; silence in backward window [540s, 600s]."""
        silence = [SilenceRange(start_ms=559_000, end_ms=561_000)]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 560_000

    def test_backward_floor_clamps(self):
        """Backward search is clamped by minimum — silence below floor is ignored."""
        silence = [SilenceRange(start_ms=530_000, end_ms=532_000)]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS, minimum=550_000)
        assert cut == 720_000  # hard cut, backward floor blocked the only candidate

    def test_picks_longest_in_round(self):
        """Multiple silences in the same round → pick longest."""
        silence = [
            SilenceRange(start_ms=610_000, end_ms=610_500),  # 500ms — too short
            SilenceRange(start_ms=630_000, end_ms=633_000),  # 3000ms ← winner
            SilenceRange(start_ms=650_000, end_ms=651_000),  # 1000ms
        ]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 631_500

    def test_ignores_short_silence_in_primary_rounds(self):
        """Silence < primary_silence (800ms) is skipped in main rounds."""
        silence = [SilenceRange(start_ms=620_000, end_ms=620_500)]  # 500ms < 800ms
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        # Falls through to fallback round which accepts >= 300ms
        assert cut == 620_250

    def test_fallback_finds_short_silence(self):
        """No silence >= 800ms anywhere, but a 400ms gap exists → fallback picks it."""
        silence = [SilenceRange(start_ms=700_000, end_ms=700_400)]  # 400ms
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 700_200

    def test_fallback_picks_longest_short_silence(self):
        """Multiple short silences in fallback range → pick longest."""
        silence = [
            SilenceRange(start_ms=620_000, end_ms=620_400),  # 400ms
            SilenceRange(start_ms=700_000, end_ms=700_600),  # 600ms ← winner
            SilenceRange(start_ms=750_000, end_ms=750_350),  # 350ms
        ]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 700_300

    def test_hard_cut_when_no_silence(self):
        """No silence at all → hard cut at maximum."""
        cut = _find_best_cut(0, [], **CUT_DEFAULTS)
        assert cut == 720_000

    def test_hard_cut_when_silence_too_short(self):
        """Silence exists but < fallback threshold → hard cut."""
        silence = [SilenceRange(start_ms=650_000, end_ms=650_200)]  # 200ms < 300ms
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 720_000

    def test_offset_position(self):
        """Search windows shift correctly with non-zero pos."""
        silence = [SilenceRange(start_ms=1_219_000, end_ms=1_221_000)]
        cut = _find_best_cut(600_000, silence, **CUT_DEFAULTS)
        assert cut == 1_220_000

    def test_forward_preferred_over_backward(self):
        """Forward round 0 hit is preferred over backward round even if backward is longer."""
        silence = [
            SilenceRange(start_ms=620_000, end_ms=621_000),  # forward round 0, 1s
            SilenceRange(start_ms=559_000, end_ms=566_000),  # backward round, 7s
        ]
        cut = _find_best_cut(0, silence, **CUT_DEFAULTS)
        assert cut == 620_500  # forward round 0 wins (tried first)

    def test_forward_clamped_to_maximum(self):
        """Forward search beyond maximum is clamped — silence past max is not found."""
        silence = [SilenceRange(start_ms=730_000, end_ms=732_000)]
        cut = _find_best_cut(0, silence, **{**CUT_DEFAULTS, "rounds": 5})
        assert cut == 720_000  # hard cut, not 731_000


# ---------------------------------------------------------------------------
# plan_segments — progressive search integration
# ---------------------------------------------------------------------------

PLAN_DEFAULTS = dict(
    target_duration_ms=600_000,
    min_duration_ms=120_000,
    max_duration_ms=720_000,
    overlap_ms=400,
    search_step_ms=60_000,
    search_max_rounds=3,
    fallback_silence_ms=300,
    min_silence_ms=800,
)


@pytest.mark.unit
class TestPlanSegments:
    def test_short_audio_single_segment(self):
        """Audio shorter than max → single segment, no splitting."""
        plans = plan_segments(500_000, [], **PLAN_DEFAULTS)
        assert len(plans) == 1
        p = plans[0]
        assert p.segment_index == 0
        assert p.source_start_ms == 0
        assert p.source_end_ms == 500_000
        assert p.keep_start_ms == 0
        assert p.keep_end_ms == 500_000

    def test_exact_max_duration(self):
        """Duration == max_duration → single segment."""
        plans = plan_segments(720_000, [], **PLAN_DEFAULTS)
        assert len(plans) == 1

    def test_split_round1_silence(self):
        """Silence in round 1 [600s, 660s] → clean 2-segment split."""
        silence = [SilenceRange(start_ms=619_000, end_ms=621_000)]
        plans = plan_segments(1_400_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].keep_end_ms == 620_000
        assert plans[1].keep_start_ms == 620_000
        assert plans[1].keep_end_ms == 1_400_000

    def test_split_round2_silence(self):
        """No round 1 match; silence in round 2 [660s, 720s]."""
        silence = [SilenceRange(start_ms=699_000, end_ms=701_000)]
        plans = plan_segments(1_500_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].keep_end_ms == 700_000
        assert plans[1].keep_start_ms == 700_000

    def test_split_backward_search(self):
        """Silence only in backward window [540s, 600s] → backward search finds it."""
        silence = [SilenceRange(start_ms=569_000, end_ms=571_000)]
        plans = plan_segments(1_400_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].keep_end_ms == 570_000

    def test_fallback_short_silence(self):
        """No long silence, but 500ms gap in range → fallback picks it."""
        silence = [SilenceRange(start_ms=650_000, end_ms=650_500)]  # 500ms
        plans = plan_segments(1_400_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].keep_end_ms == 650_250

    def test_picks_longest_silence_in_round(self):
        """Multiple silences in round 1 → pick the longest."""
        silence = [
            SilenceRange(start_ms=610_000, end_ms=611_000),  # 1s
            SilenceRange(start_ms=630_000, end_ms=633_000),  # 3s ← winner
            SilenceRange(start_ms=650_000, end_ms=651_000),  # 1s
        ]
        plans = plan_segments(1_400_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].keep_end_ms == 631_500

    def test_no_silence_hard_cut(self):
        """No silence → hard cut at max_duration (720s)."""
        plans = plan_segments(1_000_000, [], **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].keep_end_ms == 720_000
        assert plans[1].keep_start_ms == 720_000
        assert plans[1].keep_end_ms == 1_000_000

    def test_three_segments_for_long_audio(self):
        """2000s audio with silences in round 1 windows → 3 segments."""
        silence = [
            SilenceRange(start_ms=619_000, end_ms=622_000),
            SilenceRange(start_ms=1_240_000, end_ms=1_243_000),
        ]
        plans = plan_segments(2_000_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 3
        assert plans[0].keep_end_ms == 620_500
        assert plans[1].keep_start_ms == 620_500
        assert plans[1].keep_end_ms == 1_241_500
        assert plans[2].keep_start_ms == 1_241_500
        assert plans[2].keep_end_ms == 2_000_000

    def test_tiny_trailing_merged(self):
        """If cutting would leave < min_duration, trailing is merged."""
        # 830s audio: cut at 720s hard → 110s < 120s min → single segment
        plans = plan_segments(830_000, [], **PLAN_DEFAULTS)
        assert len(plans) == 1
        assert plans[0].keep_end_ms == 830_000

    def test_silence_before_search_window_ignored(self):
        """Silence before the backward search floor is not used."""
        silence = [SilenceRange(start_ms=100_000, end_ms=101_000)]
        plans = plan_segments(1_600_000, silence, **PLAN_DEFAULTS)
        assert plans[0].keep_end_ms == 720_000  # hard cut

    def test_silence_after_search_window_ignored(self):
        """Silence after the search window (> max) is not used."""
        silence = [SilenceRange(start_ms=730_000, end_ms=731_000)]
        plans = plan_segments(1_600_000, silence, **PLAN_DEFAULTS)
        assert plans[0].keep_end_ms == 720_000  # hard cut

    def test_keep_regions_cover_full_duration(self):
        """keep regions must tile the entire audio without gaps or overlaps."""
        silence = [
            SilenceRange(start_ms=619_000, end_ms=622_000),
            SilenceRange(start_ms=1_240_000, end_ms=1_244_000),
            SilenceRange(start_ms=1_860_000, end_ms=1_864_000),
        ]
        total = 2_400_000
        plans = plan_segments(total, silence, **PLAN_DEFAULTS)

        assert plans[0].keep_start_ms == 0
        for i in range(1, len(plans)):
            assert plans[i].keep_start_ms == plans[i - 1].keep_end_ms, (
                f"Gap between segment {i-1} and {i}"
            )
        assert plans[-1].keep_end_ms == total

    def test_overlap_symmetric(self):
        """Middle segments have overlap on both sides."""
        silence = [
            SilenceRange(start_ms=619_000, end_ms=622_000),
            SilenceRange(start_ms=1_240_000, end_ms=1_244_000),
        ]
        plans = plan_segments(2_400_000, silence, **PLAN_DEFAULTS)

        mid = plans[1]
        assert mid.source_start_ms == mid.keep_start_ms - 400
        assert mid.source_end_ms == mid.keep_end_ms + 400

    def test_first_segment_no_leading_overlap(self):
        """First segment starts at 0, no leading overlap."""
        silence = [SilenceRange(start_ms=619_000, end_ms=622_000)]
        plans = plan_segments(1_600_000, silence, **PLAN_DEFAULTS)
        assert plans[0].source_start_ms == 0

    def test_last_segment_no_trailing_overlap(self):
        """Last segment ends at total_duration, no trailing overlap."""
        silence = [SilenceRange(start_ms=619_000, end_ms=622_000)]
        plans = plan_segments(1_600_000, silence, **PLAN_DEFAULTS)
        assert plans[-1].source_end_ms == 1_600_000

    def test_many_segments_long_audio(self):
        """60-minute (3600s) audio → multiple segments, all valid."""
        total = 3_600_000
        silence = [
            SilenceRange(start_ms=i * 620_000 - 1000, end_ms=i * 620_000 + 1000)
            for i in range(1, 6)
        ]
        plans = plan_segments(total, silence, **PLAN_DEFAULTS)

        assert len(plans) >= 3
        assert plans[0].keep_start_ms == 0
        assert plans[-1].keep_end_ms == total

        for i in range(1, len(plans)):
            assert plans[i].keep_start_ms == plans[i - 1].keep_end_ms

    def test_20_minute_audio_two_segments(self):
        """20-minute (1200s) audio with silence at 620s → 2 segments."""
        silence = [
            SilenceRange(start_ms=619_000, end_ms=622_000),
        ]
        plans = plan_segments(1_200_000, silence, **PLAN_DEFAULTS)

        assert len(plans) == 2
        assert plans[0].source_start_ms == 0
        assert plans[0].source_end_ms == 620_900
        assert plans[0].keep_start_ms == 0
        assert plans[0].keep_end_ms == 620_500

        assert plans[1].source_start_ms == 620_100
        assert plans[1].source_end_ms == 1_200_000
        assert plans[1].keep_start_ms == 620_500
        assert plans[1].keep_end_ms == 1_200_000

    def test_audio_over_max_with_enough_trailing(self):
        """Audio over max with enough trailing → 2 segments."""
        # 860s audio: cut at 720s, trailing 140s > 120s → split
        plans = plan_segments(860_000, [], **PLAN_DEFAULTS)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 720_000
        assert plans[1].keep_end_ms == 860_000

    def test_progressive_search_escalation(self):
        """Short silence (500ms) at 700s — fails primary rounds, fallback picks it."""
        silence = [SilenceRange(start_ms=699_750, end_ms=700_250)]  # 500ms at 700s
        plans = plan_segments(1_400_000, silence, **PLAN_DEFAULTS)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 700_000

    def test_sentence_gap_detection(self):
        """A 350ms sentence-level gap (>= 300ms fallback) is used when no long silence exists."""
        silence = [SilenceRange(start_ms=650_000, end_ms=650_350)]
        plans = plan_segments(1_400_000, silence, **PLAN_DEFAULTS)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 650_175


# ---------------------------------------------------------------------------
# Segment level presets (10m / 20m / 30m)
# ---------------------------------------------------------------------------

from app.config import SEGMENT_LEVEL_PRESETS, SegmentLevelPreset

PLAN_20M = dict(
    target_duration_ms=1_200_000,
    min_duration_ms=120_000,
    max_duration_ms=1_440_000,
    overlap_ms=400,
    search_step_ms=120_000,
    search_max_rounds=3,
    fallback_silence_ms=300,
    min_silence_ms=800,
)

PLAN_30M = dict(
    target_duration_ms=1_800_000,
    min_duration_ms=120_000,
    max_duration_ms=2_160_000,
    overlap_ms=400,
    search_step_ms=180_000,
    search_max_rounds=3,
    fallback_silence_ms=300,
    min_silence_ms=800,
)


@pytest.mark.unit
class TestSegmentLevelPresets:
    """Verify the SEGMENT_LEVEL_PRESETS configuration dictionary."""

    def test_presets_keys(self):
        assert set(SEGMENT_LEVEL_PRESETS.keys()) == {"10m", "20m", "30m"}

    def test_10m_preset_values(self):
        p = SEGMENT_LEVEL_PRESETS["10m"]
        assert p.target_duration_sec == 600
        assert p.max_duration_sec == 720
        assert p.min_file_duration_sec == 720
        assert p.search_step_sec == 60

    def test_20m_preset_values(self):
        p = SEGMENT_LEVEL_PRESETS["20m"]
        assert p.target_duration_sec == 1200
        assert p.max_duration_sec == 1440
        assert p.min_file_duration_sec == 1440
        assert p.search_step_sec == 120

    def test_30m_preset_values(self):
        p = SEGMENT_LEVEL_PRESETS["30m"]
        assert p.target_duration_sec == 1800
        assert p.max_duration_sec == 2160
        assert p.min_file_duration_sec == 2160
        assert p.search_step_sec == 180

    def test_preset_is_frozen(self):
        p = SEGMENT_LEVEL_PRESETS["10m"]
        with pytest.raises(AttributeError):
            p.target_duration_sec = 999


@pytest.mark.unit
class TestPlanSegments20m:
    """plan_segments with 20-minute level parameters."""

    def test_20m_no_split_under_max(self):
        """20-minute audio (1200s) fits within 1440s max → single segment."""
        plans = plan_segments(1_200_000, [], **PLAN_20M)
        assert len(plans) == 1
        assert plans[0].keep_end_ms == 1_200_000

    def test_20m_no_split_at_max(self):
        """Audio at exactly 1440s (24 min) → single segment."""
        plans = plan_segments(1_440_000, [], **PLAN_20M)
        assert len(plans) == 1

    def test_20m_split_40min_audio(self):
        """40-minute (2400s) audio with silence at ~1220s → 2 segments, cut in 20~24m window."""
        silence = [SilenceRange(start_ms=1_219_000, end_ms=1_222_000)]
        plans = plan_segments(2_400_000, silence, **PLAN_20M)
        assert len(plans) == 2
        cut_ms = plans[0].keep_end_ms
        assert 1_200_000 <= cut_ms <= 1_440_000

    def test_20m_hard_cut_no_silence(self):
        """No silence → hard cut at 1440s for 20m level."""
        plans = plan_segments(2_900_000, [], **PLAN_20M)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 1_440_000

    def test_20m_forward_round2(self):
        """Silence only in forward round 2 window [1320s, 1440s] (step=120s)."""
        silence = [SilenceRange(start_ms=1_379_000, end_ms=1_381_000)]
        plans = plan_segments(2_900_000, silence, **PLAN_20M)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 1_380_000

    def test_20m_trailing_merge(self):
        """Trailing fragment < min_duration merged with previous segment."""
        # 1550s audio: hard cut at 1440s → trailing 110s < 120s → single segment
        plans = plan_segments(1_550_000, [], **PLAN_20M)
        assert len(plans) == 1

    def test_20m_keep_regions_tile(self):
        """Keep regions tile the entire audio with no gaps."""
        silence = [
            SilenceRange(start_ms=1_219_000, end_ms=1_222_000),
            SilenceRange(start_ms=2_439_000, end_ms=2_442_000),
        ]
        total = 3_600_000
        plans = plan_segments(total, silence, **PLAN_20M)
        assert plans[0].keep_start_ms == 0
        for i in range(1, len(plans)):
            assert plans[i].keep_start_ms == plans[i - 1].keep_end_ms
        assert plans[-1].keep_end_ms == total


@pytest.mark.unit
class TestPlanSegments30m:
    """plan_segments with 30-minute level parameters."""

    def test_30m_no_split_under_max(self):
        """30-minute audio (1800s) fits within 2160s max → single segment."""
        plans = plan_segments(1_800_000, [], **PLAN_30M)
        assert len(plans) == 1
        assert plans[0].keep_end_ms == 1_800_000

    def test_30m_no_split_at_max(self):
        """Audio at exactly 2160s (36 min) → single segment."""
        plans = plan_segments(2_160_000, [], **PLAN_30M)
        assert len(plans) == 1

    def test_30m_split_60min_audio(self):
        """60-minute (3600s) audio with silence at ~1820s → 2 segments, cut in 30~36m window."""
        silence = [SilenceRange(start_ms=1_819_000, end_ms=1_822_000)]
        plans = plan_segments(3_600_000, silence, **PLAN_30M)
        assert len(plans) == 2
        cut_ms = plans[0].keep_end_ms
        assert 1_800_000 <= cut_ms <= 2_160_000

    def test_30m_hard_cut_no_silence(self):
        """No silence → hard cut at 2160s for 30m level."""
        plans = plan_segments(4_400_000, [], **PLAN_30M)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 2_160_000

    def test_30m_forward_round2(self):
        """Silence only in forward round 2 window [1980s, 2160s] (step=180s)."""
        silence = [SilenceRange(start_ms=2_059_000, end_ms=2_061_000)]
        plans = plan_segments(4_200_000, silence, **PLAN_30M)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 2_060_000

    def test_30m_trailing_merge(self):
        """Trailing fragment < min_duration merged with previous segment."""
        # 2270s audio: hard cut at 2160s → trailing 110s < 120s → single segment
        plans = plan_segments(2_270_000, [], **PLAN_30M)
        assert len(plans) == 1

    def test_30m_fallback_short_silence(self):
        """30m level: 400ms gap in [1620s, 2160s] → fallback picks it."""
        silence = [SilenceRange(start_ms=1_850_000, end_ms=1_850_400)]
        plans = plan_segments(3_800_000, silence, **PLAN_30M)
        assert len(plans) == 2
        assert plans[0].keep_end_ms == 1_850_200

    def test_30m_keep_regions_tile(self):
        """Keep regions tile the entire audio with no gaps."""
        silence = [
            SilenceRange(start_ms=1_819_000, end_ms=1_822_000),
            SilenceRange(start_ms=3_639_000, end_ms=3_642_000),
        ]
        total = 5_400_000
        plans = plan_segments(total, silence, **PLAN_30M)
        assert plans[0].keep_start_ms == 0
        for i in range(1, len(plans)):
            assert plans[i].keep_start_ms == plans[i - 1].keep_end_ms
        assert plans[-1].keep_end_ms == total
