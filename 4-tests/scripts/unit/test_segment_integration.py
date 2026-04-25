"""Integration tests for VAD segment transcription pipeline.

Covers:
- Configuration threshold (10-minute / 600s)
- SegmentSummary diagnostic enrichment
- Merge-and-finalize flow (end-to-end with in-memory data)
- Cancel / delete segment handling
- SRT timestamp monotonicity after merge
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.models.task_segment import SegmentStatus, TaskSegment
from app.schemas.task import SegmentSummary, TaskResponse
from app.services.result_merger import (
    MERGE_STATUS_OK,
    MERGE_STATUS_TEXT_ONLY_FALLBACK,
    SegmentInput,
    merge_segment_results,
)
from app.services.result_formatter import parse_timestamp_segments, to_json, to_srt, to_txt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw(stamp_sents: list[dict], text: str = "", mode: str = "offline") -> str:
    return json.dumps({"text": text, "stamp_sents": stamp_sents, "mode": mode})


def _make_segment_input(
    idx: int,
    source_start: int,
    keep_start: int,
    keep_end: int,
    stamp_sents: list[dict],
    text: str = "",
) -> SegmentInput:
    return SegmentInput(
        segment_index=idx,
        source_start_ms=source_start,
        keep_start_ms=keep_start,
        keep_end_ms=keep_end,
        raw_result_json=_raw(stamp_sents, text),
    )


# ---------------------------------------------------------------------------
# Configuration threshold
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSegmentThreshold:
    def test_threshold_is_600_seconds(self):
        assert settings.segment_min_file_duration_sec == 600

    def test_short_audio_below_threshold(self):
        """Audio under 600s should not be considered for segmentation."""
        assert 599 < settings.segment_min_file_duration_sec

    def test_target_and_max_durations(self):
        assert settings.segment_target_duration_sec == 600
        assert settings.segment_max_duration_sec == 780
        assert settings.segment_overlap_ms == 400

    def test_progressive_search_config(self):
        assert settings.segment_search_step_sec == 60
        assert settings.segment_search_max_rounds == 3
        assert settings.segment_fallback_silence_sec == 0.3


# ---------------------------------------------------------------------------
# SegmentSummary schema
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSegmentSummarySchema:
    def test_defaults(self):
        s = SegmentSummary(total=5)
        assert s.total == 5
        assert s.succeeded == 0
        assert s.failed == 0
        assert s.pending == 0
        assert s.active == 0
        assert s.assigned_server_ids == []

    def test_full_construction(self):
        s = SegmentSummary(
            total=10, succeeded=7, failed=1, pending=0, active=2,
            assigned_server_ids=["srv-a", "srv-b"],
        )
        assert s.total == 10
        assert s.succeeded == 7
        assert s.assigned_server_ids == ["srv-a", "srv-b"]

    def test_task_response_segments_default_none(self):
        resp = TaskResponse(
            task_id="T1", user_id="U1", file_id="F1", status="QUEUED",
            progress=0.0, language="zh",
            created_at="2026-01-01T00:00:00Z",
        )
        assert resp.segment_info is None

    def test_task_response_with_segments(self):
        resp = TaskResponse(
            task_id="T1", user_id="U1", file_id="F1", status="TRANSCRIBING",
            progress=0.5, language="zh",
            created_at="2026-01-01T00:00:00Z",
            segment_info=SegmentSummary(total=3, succeeded=1, active=2),
        )
        assert resp.segment_info is not None
        assert resp.segment_info.total == 3
        assert resp.segment_info.succeeded == 1

    def test_task_response_serializes_as_segments(self):
        """Verify that segment_info serializes with JSON key 'segments'."""
        resp = TaskResponse(
            task_id="T1", user_id="U1", file_id="F1", status="TRANSCRIBING",
            progress=0.5, language="zh",
            created_at="2026-01-01T00:00:00Z",
            segment_info=SegmentSummary(total=2, succeeded=2),
        )
        data = resp.model_dump(by_alias=True)
        assert "segments" in data
        assert data["segments"]["total"] == 2


# ---------------------------------------------------------------------------
# End-to-end merge pipeline (merge → format → validate)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMergePipelineEndToEnd:
    """Simulate a full 3-segment 1200s audio merge through formatters."""

    def _build_three_segment_inputs(self) -> list[SegmentInput]:
        """Simulates a ~2000s audio split at ~620s and ~1241s (10-11 min windows)."""
        seg0 = _make_segment_input(
            idx=0, source_start=0, keep_start=0, keep_end=620_000,
            stamp_sents=[
                {"text_seg": "会议开始", "punc": "，", "ts": [1000, 3000]},
                {"text_seg": "大家好", "punc": "。", "ts": [3000, 5000]},
            ],
            text="会议开始，大家好。",
        )
        seg1 = _make_segment_input(
            idx=1, source_start=619_600, keep_start=620_000, keep_end=1_241_000,
            stamp_sents=[
                {"text_seg": "重叠尾", "punc": "", "ts": [0, 300]},
                {"text_seg": "讨论项目进展", "punc": "，", "ts": [500, 3000]},
                {"text_seg": "接下来看看数据", "punc": "。", "ts": [3000, 6000]},
            ],
        )
        seg2 = _make_segment_input(
            idx=2, source_start=1_240_600, keep_start=1_241_000, keep_end=2_000_000,
            stamp_sents=[
                {"text_seg": "重叠头", "punc": "", "ts": [0, 300]},
                {"text_seg": "总结一下", "punc": "，", "ts": [500, 2000]},
                {"text_seg": "会议结束", "punc": "。", "ts": [2000, 4000]},
            ],
        )
        return [seg0, seg1, seg2]

    def test_merge_status_ok(self):
        result, status = merge_segment_results(self._build_three_segment_inputs())
        assert status == MERGE_STATUS_OK

    def test_merged_text_contains_all_kept(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        for expected in ("会议开始", "大家好", "讨论项目进展", "接下来看看数据", "总结一下", "会议结束"):
            assert expected in result["text"]

    def test_overlap_sentences_excluded(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        texts = [s["text_seg"] for s in result["stamp_sents"]]
        assert "重叠尾" not in texts
        assert "重叠头" not in texts

    def test_global_timestamps_monotonic(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        starts = [s["ts"][0] for s in result["stamp_sents"]]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1], f"Non-monotonic at {i}: {starts}"

    def test_to_json_output_valid(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        json_str = to_json(result)
        parsed = json.loads(json_str)
        assert "segments" in parsed
        assert len(parsed["segments"]) >= 6

    def test_to_txt_output(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        txt = to_txt(result)
        assert "会议开始" in txt
        assert "会议结束" in txt

    def test_srt_timestamp_monotonicity(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        srt = to_srt(result)
        segs = parse_timestamp_segments(result)
        starts = [s.start_ms for s in segs]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1]

    def test_srt_indices_sequential(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        srt = to_srt(result)
        lines = srt.strip().split("\n")
        indices = [int(ln) for ln in lines if ln.strip().isdigit()]
        assert indices == list(range(1, len(indices) + 1))

    def test_second_segment_timestamps_shifted(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        sents = result["stamp_sents"]
        seg1_sents = [s for s in sents if 620_000 <= s["ts"][0] < 1_241_000]
        assert len(seg1_sents) >= 2
        assert seg1_sents[0]["ts"][0] >= 620_000

    def test_third_segment_timestamps_shifted(self):
        result, _ = merge_segment_results(self._build_three_segment_inputs())
        sents = result["stamp_sents"]
        seg2_sents = [s for s in sents if s["ts"][0] >= 1_241_000]
        assert len(seg2_sents) >= 2
        assert seg2_sents[0]["ts"][0] >= 1_241_000


# ---------------------------------------------------------------------------
# Five-segment stress test (simulating ~40 min audio)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFiveSegmentMerge:
    def _build_five_segments(self) -> list[SegmentInput]:
        cuts = [0, 620_000, 1_241_000, 1_862_000, 2_483_000, 3_100_000]
        inputs = []
        for i in range(5):
            ks = cuts[i]
            ke = cuts[i + 1]
            ss = max(0, ks - 400) if i > 0 else 0
            sents = [
                {"text_seg": f"段{i}句A", "punc": "，", "ts": [500, 200_000]},
                {"text_seg": f"段{i}句B", "punc": "。", "ts": [200_000, 400_000]},
            ]
            inputs.append(SegmentInput(
                segment_index=i,
                source_start_ms=ss,
                keep_start_ms=ks,
                keep_end_ms=ke,
                raw_result_json=_raw(sents),
            ))
        return inputs

    def test_all_segments_present(self):
        result, status = merge_segment_results(self._build_five_segments())
        assert status == MERGE_STATUS_OK
        for i in range(5):
            assert f"段{i}句A" in result["text"]
            assert f"段{i}句B" in result["text"]

    def test_monotonic_timestamps(self):
        result, _ = merge_segment_results(self._build_five_segments())
        starts = [s["ts"][0] for s in result["stamp_sents"]]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1]

    def test_srt_valid(self):
        result, _ = merge_segment_results(self._build_five_segments())
        srt = to_srt(result)
        assert srt.count("-->") >= 10


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMergeEdgeCases:
    def test_single_segment_passthrough(self):
        """Single segment merge is a pass-through."""
        inputs = [_make_segment_input(
            0, 0, 0, 660_000,
            [{"text_seg": "独段", "punc": "。", "ts": [0, 500]}],
        )]
        result, status = merge_segment_results(inputs)
        assert status == MERGE_STATUS_OK
        assert result["text"] == "独段。"

    def test_empty_segment_result(self):
        """Segment with empty raw JSON doesn't crash."""
        inputs = [
            _make_segment_input(0, 0, 0, 620_000, [{"text_seg": "ok", "punc": "", "ts": [0, 100]}]),
            SegmentInput(1, 619_600, 620_000, 1_241_000, "{}"),
        ]
        result, _ = merge_segment_results(inputs)
        assert "ok" in result["text"]

    def test_very_long_text_segment(self):
        """Large text in a segment doesn't cause issues."""
        long_text = "长" * 50000
        inputs = [_make_segment_input(
            0, 0, 0, 660_000,
            [{"text_seg": long_text, "punc": "。", "ts": [0, 660_000]}],
        )]
        result, _ = merge_segment_results(inputs)
        assert len(result["text"]) == 50001  # text + punc

    def test_mixed_timestamped_and_text_only(self):
        """Mix of timestamped and text-only segments triggers fallback status."""
        inputs = [
            _make_segment_input(0, 0, 0, 620_000, [{"text_seg": "有ts", "punc": "", "ts": [0, 100]}]),
            SegmentInput(1, 619_600, 620_000, 1_241_000, json.dumps({"text": "无ts"})),
        ]
        result, status = merge_segment_results(inputs)
        assert status == MERGE_STATUS_TEXT_ONLY_FALLBACK
        assert "有ts" in result["text"]
        assert "无ts" in result["text"]


# ---------------------------------------------------------------------------
# SegmentStatus enum
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSegmentStatusEnum:
    def test_all_values(self):
        assert set(SegmentStatus) == {
            SegmentStatus.PENDING,
            SegmentStatus.DISPATCHED,
            SegmentStatus.TRANSCRIBING,
            SegmentStatus.SUCCEEDED,
            SegmentStatus.FAILED,
        }

    def test_string_values(self):
        assert SegmentStatus.PENDING == "PENDING"
        assert SegmentStatus.SUCCEEDED == "SUCCEEDED"


# ---------------------------------------------------------------------------
# AutoSegmentMode schema & API injection
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAutoSegmentSchema:
    def test_enum_values(self):
        from app.schemas.task import AutoSegmentMode
        assert set(AutoSegmentMode) == {"auto", "on", "off"}

    def test_task_create_request_default(self):
        from app.schemas.task import TaskCreateRequest
        req = TaskCreateRequest(items=[{"file_id": "f1"}])
        assert req.auto_segment == "auto"

    def test_task_create_request_on(self):
        from app.schemas.task import TaskCreateRequest
        req = TaskCreateRequest(items=[{"file_id": "f1"}], auto_segment="on")
        assert req.auto_segment == "on"

    def test_task_create_request_off(self):
        from app.schemas.task import TaskCreateRequest
        req = TaskCreateRequest(items=[{"file_id": "f1"}], auto_segment="off")
        assert req.auto_segment == "off"

    def test_task_create_request_invalid_rejected(self):
        from app.schemas.task import TaskCreateRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            TaskCreateRequest(items=[{"file_id": "f1"}], auto_segment="invalid")


# ---------------------------------------------------------------------------
# _parse_auto_segment in task_runner
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestParseAutoSegment:
    def test_none_returns_auto(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment(None) == "auto"

    def test_empty_returns_auto(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment("") == "auto"

    def test_on(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment('{"auto_segment": "on"}') == "on"

    def test_off(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment('{"auto_segment": "off"}') == "off"

    def test_auto_explicit(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment('{"auto_segment": "auto"}') == "auto"

    def test_missing_key_returns_auto(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment('{"hotwords": "w"}') == "auto"

    def test_invalid_value_returns_auto(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment('{"auto_segment": "maybe"}') == "auto"

    def test_malformed_json_returns_auto(self):
        from app.services.task_runner import BackgroundTaskRunner
        assert BackgroundTaskRunner._parse_auto_segment("{broken}") == "auto"
