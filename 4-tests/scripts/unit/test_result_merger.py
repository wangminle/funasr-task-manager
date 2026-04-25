"""Unit tests for segment result merger."""

import json

import pytest

from app.services.result_merger import (
    MERGE_STATUS_OK,
    MERGE_STATUS_TEXT_ONLY_FALLBACK,
    SegmentInput,
    _filter_and_offset,
    merge_segment_results,
)
from app.services.result_formatter import (
    TimestampSegment,
    parse_timestamp_segments,
    to_json,
    to_srt,
    to_txt,
)


def _raw(stamp_sents: list[dict], text: str = "", mode: str = "offline") -> str:
    """Build a raw FunASR result JSON string."""
    return json.dumps({"text": text, "stamp_sents": stamp_sents, "mode": mode})


# ---------------------------------------------------------------------------
# _filter_and_offset
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFilterAndOffset:
    def test_basic_offset(self):
        parsed = [
            TimestampSegment(start_ms=0, end_ms=500, text="hello"),
            TimestampSegment(start_ms=500, end_ms=1000, text="world"),
        ]
        result = _filter_and_offset(parsed, source_start_ms=620_000, keep_start_ms=620_000, keep_end_ms=1_241_000)
        assert len(result) == 2
        assert result[0].start_ms == 620_000
        assert result[0].end_ms == 620_500
        assert result[1].start_ms == 620_500

    def test_overlap_region_filtered(self):
        """Sentences in the overlap region (before keep_start) are excluded."""
        # Segment starts at source=619600, keep starts at 620000
        # A sentence at local time 0ms → global 619600ms < 620000 → filtered out
        parsed = [
            TimestampSegment(start_ms=0, end_ms=300, text="overlap"),
            TimestampSegment(start_ms=400, end_ms=800, text="kept"),
        ]
        result = _filter_and_offset(
            parsed,
            source_start_ms=619_600,
            keep_start_ms=620_000,
            keep_end_ms=1_241_000,
        )
        assert len(result) == 1
        assert result[0].text == "kept"
        assert result[0].start_ms == 620_000

    def test_trailing_overlap_filtered(self):
        """Sentences at or after keep_end are excluded."""
        parsed = [
            TimestampSegment(start_ms=0, end_ms=200, text="in"),
            TimestampSegment(start_ms=620_000, end_ms=620_500, text="out"),
        ]
        result = _filter_and_offset(
            parsed,
            source_start_ms=0,
            keep_start_ms=0,
            keep_end_ms=620_000,
        )
        assert len(result) == 1
        assert result[0].text == "in"

    def test_boundary_sentence_at_keep_start(self):
        """A sentence starting exactly at keep_start is included."""
        parsed = [TimestampSegment(start_ms=400, end_ms=900, text="boundary")]
        result = _filter_and_offset(
            parsed, source_start_ms=619_600, keep_start_ms=620_000, keep_end_ms=1_241_000,
        )
        assert len(result) == 1
        assert result[0].start_ms == 620_000

    def test_empty_input(self):
        assert _filter_and_offset([], 0, 0, 620_000) == []


# ---------------------------------------------------------------------------
# merge_segment_results
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMergeSegmentResults:
    def test_empty_input(self):
        result, status = merge_segment_results([])
        assert result["text"] == ""
        assert result["stamp_sents"] == []
        assert status == MERGE_STATUS_OK

    def test_single_segment(self):
        raw = _raw([
            {"text_seg": "你好", "punc": "，", "ts": [0, 500]},
            {"text_seg": "世界", "punc": "。", "ts": [500, 1000]},
        ], text="你好，世界。")

        inputs = [SegmentInput(
            segment_index=0,
            source_start_ms=0,
            keep_start_ms=0,
            keep_end_ms=660_000,
            raw_result_json=raw,
        )]
        result, status = merge_segment_results(inputs)

        assert status == MERGE_STATUS_OK
        assert "你好" in result["text"]
        assert "世界" in result["text"]
        assert len(result["stamp_sents"]) == 2
        assert result["stamp_sents"][0]["ts"] == [0, 500]

    def test_two_segments_with_offset(self):
        """Two segments, second gets timestamps shifted past overlap."""
        raw0 = _raw([
            {"text_seg": "第一段", "punc": "。", "ts": [0, 2000]},
        ], text="第一段。")

        # local ts=400 → global 619600+400=620000 → at keep_start → included
        raw1 = _raw([
            {"text_seg": "第二段", "punc": "。", "ts": [400, 2400]},
        ], text="第二段。")

        inputs = [
            SegmentInput(
                segment_index=0,
                source_start_ms=0,
                keep_start_ms=0,
                keep_end_ms=620_000,
                raw_result_json=raw0,
            ),
            SegmentInput(
                segment_index=1,
                source_start_ms=619_600,
                keep_start_ms=620_000,
                keep_end_ms=1_241_000,
                raw_result_json=raw1,
            ),
        ]
        result, status = merge_segment_results(inputs)

        assert status == MERGE_STATUS_OK
        assert result["text"] == "第一段。第二段。"
        sents = result["stamp_sents"]
        assert len(sents) == 2
        assert sents[0]["ts"] == [0, 2000]
        assert sents[1]["ts"] == [620_000, 622_000]  # 400+619600, 2400+619600

    def test_overlap_deduplication(self):
        """Sentences in the overlap region appear in only one segment."""
        # Segment 0: source [0, 620400], keep [0, 620000]
        raw0 = _raw([
            {"text_seg": "正文", "punc": "。", "ts": [0, 2000]},
            {"text_seg": "重叠区", "punc": "。", "ts": [620_000, 620_300]},
        ])
        # Segment 1: source [619600, 1241000], keep [620000, 1241000]
        raw1 = _raw([
            {"text_seg": "重叠区", "punc": "。", "ts": [0, 300]},
            {"text_seg": "继续", "punc": "。", "ts": [400, 800]},
        ])

        inputs = [
            SegmentInput(0, source_start_ms=0, keep_start_ms=0, keep_end_ms=620_000, raw_result_json=raw0),
            SegmentInput(1, source_start_ms=619_600, keep_start_ms=620_000, keep_end_ms=1_241_000, raw_result_json=raw1),
        ]
        result, _ = merge_segment_results(inputs)

        texts = [s["text_seg"] for s in result["stamp_sents"]]
        # "正文" in seg0 keep region, "重叠区" at 620000 in seg0 is at keep boundary → excluded from seg0
        # In seg1: "重叠区" local ts=0 → global 619600 < 620000 → excluded
        # "继续" local ts=400 → global 620000 → included
        assert "正文。" in texts
        assert "继续。" in texts
        assert texts.count("重叠区。") == 0

    def test_monotonic_timestamps(self):
        """Merged timestamps must be monotonically non-decreasing."""
        raw0 = _raw([
            {"text_seg": "A", "punc": "", "ts": [0, 1000]},
            {"text_seg": "B", "punc": "", "ts": [1000, 2000]},
        ])
        raw1 = _raw([
            {"text_seg": "C", "punc": "", "ts": [500, 1500]},
            {"text_seg": "D", "punc": "", "ts": [1500, 2500]},
        ])

        inputs = [
            SegmentInput(0, source_start_ms=0, keep_start_ms=0, keep_end_ms=620_000, raw_result_json=raw0),
            SegmentInput(1, source_start_ms=619_600, keep_start_ms=620_000, keep_end_ms=1_241_000, raw_result_json=raw1),
        ]
        result, _ = merge_segment_results(inputs)

        starts = [s["ts"][0] for s in result["stamp_sents"]]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1], f"Non-monotonic at index {i}: {starts}"

    def test_text_only_fallback(self):
        """Segment with no timestamps triggers TEXT_ONLY_FALLBACK."""
        raw0 = _raw([
            {"text_seg": "有时间戳", "punc": "。", "ts": [0, 2000]},
        ])
        raw1 = json.dumps({"text": "无时间戳内容"})

        inputs = [
            SegmentInput(0, source_start_ms=0, keep_start_ms=0, keep_end_ms=620_000, raw_result_json=raw0),
            SegmentInput(1, source_start_ms=619_600, keep_start_ms=620_000, keep_end_ms=1_241_000, raw_result_json=raw1),
        ]
        result, status = merge_segment_results(inputs)

        assert status == MERGE_STATUS_TEXT_ONLY_FALLBACK
        assert "有时间戳" in result["text"]
        assert "无时间戳内容" in result["text"]

    def test_all_text_only(self):
        """When all segments lack timestamps, still concatenate text."""
        raw0 = json.dumps({"text": "段落一"})
        raw1 = json.dumps({"text": "段落二"})

        inputs = [
            SegmentInput(0, 0, 0, 620_000, raw0),
            SegmentInput(1, 619_600, 620_000, 1_241_000, raw1),
        ]
        result, status = merge_segment_results(inputs)

        assert status == MERGE_STATUS_TEXT_ONLY_FALLBACK
        assert result["text"] == "段落一段落二"
        assert result["stamp_sents"] == []

    def test_unordered_input_sorted(self):
        """Segments passed out-of-order are sorted by segment_index."""
        raw0 = _raw([{"text_seg": "first", "punc": "", "ts": [0, 500]}])
        raw1 = _raw([{"text_seg": "second", "punc": "", "ts": [500, 1000]}])

        inputs = [
            SegmentInput(1, 619_600, 620_000, 1_241_000, raw1),
            SegmentInput(0, 0, 0, 620_000, raw0),
        ]
        result, _ = merge_segment_results(inputs)
        assert result["text"] == "firstsecond"

    def test_malformed_json_handled(self):
        """Segment with malformed JSON doesn't crash the merger."""
        raw0 = _raw([{"text_seg": "ok", "punc": "", "ts": [0, 500]}])

        inputs = [
            SegmentInput(0, 0, 0, 620_000, raw0),
            SegmentInput(1, 619_600, 620_000, 1_241_000, "not-json{{{"),
        ]
        result, status = merge_segment_results(inputs)
        assert "ok" in result["text"]

    def test_mode_from_first_segment(self):
        raw = _raw([{"text_seg": "x", "punc": "", "ts": [0, 100]}], mode="2pass-offline")
        inputs = [SegmentInput(0, 0, 0, 620_000, raw)]
        result, _ = merge_segment_results(inputs)
        assert result["mode"] == "2pass-offline"


# ---------------------------------------------------------------------------
# Integration: merged result → to_json / to_txt / to_srt
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestMergedResultFormatters:
    def _make_merged(self) -> dict:
        """Build a realistic merged result for formatter tests."""
        raw0 = _raw([
            {"text_seg": "开始讲话", "punc": "，", "ts": [1000, 3000]},
            {"text_seg": "第一段结束", "punc": "。", "ts": [3000, 5000]},
        ])
        raw1 = _raw([
            {"text_seg": "第二段开始", "punc": "，", "ts": [500, 2500]},
            {"text_seg": "全文结束", "punc": "。", "ts": [2500, 4500]},
        ])
        inputs = [
            SegmentInput(0, 0, 0, 620_000, raw0),
            SegmentInput(1, 619_600, 620_000, 1_241_000, raw1),
        ]
        result, _ = merge_segment_results(inputs)
        return result

    def test_to_json_roundtrip(self):
        merged = self._make_merged()
        json_str = to_json(merged)
        parsed = json.loads(json_str)
        assert len(parsed["segments"]) == 4
        assert parsed["segments"][0]["start_ms"] == 1000

    def test_to_txt(self):
        merged = self._make_merged()
        txt = to_txt(merged)
        assert "开始讲话" in txt
        assert "全文结束" in txt

    def test_to_srt(self):
        merged = self._make_merged()
        srt = to_srt(merged)
        assert "00:00:01,000 --> 00:00:03,000" in srt
        assert "开始讲话，" in srt
        lines = srt.strip().split("\n")
        assert lines[0] == "1"

    def test_srt_timestamps_monotonic(self):
        merged = self._make_merged()
        srt = to_srt(merged)
        segs = parse_timestamp_segments(merged)
        starts = [s.start_ms for s in segs]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1]


# ---------------------------------------------------------------------------
# Design-document scenario
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDesignDocScenario:
    def test_three_segment_merge(self):
        """Reproduce a 3-segment scenario with 10-11 min search window.

        Audio: 2000s, cuts at ~620s and ~1241s.
        Segment 0: source=[0, 620900] keep=[0, 620500]
        Segment 1: source=[620100, 1241900] keep=[620500, 1241500]
        Segment 2: source=[1241100, 2000000] keep=[1241500, 2000000]
        """
        raw0 = _raw([
            {"text_seg": "第一段内容", "punc": "。", "ts": [100, 619_000]},
            {"text_seg": "重叠尾", "punc": "", "ts": [620_500, 620_800]},
        ])
        raw1 = _raw([
            {"text_seg": "重叠头", "punc": "", "ts": [0, 300]},
            {"text_seg": "第二段内容", "punc": "。", "ts": [500, 619_000]},
            {"text_seg": "重叠尾2", "punc": "", "ts": [621_500, 621_600]},
        ])
        raw2 = _raw([
            {"text_seg": "重叠头2", "punc": "", "ts": [0, 300]},
            {"text_seg": "第三段内容", "punc": "。", "ts": [500, 240_000]},
        ])

        inputs = [
            SegmentInput(0, source_start_ms=0, keep_start_ms=0, keep_end_ms=620_500, raw_result_json=raw0),
            SegmentInput(1, source_start_ms=620_100, keep_start_ms=620_500, keep_end_ms=1_241_500, raw_result_json=raw1),
            SegmentInput(2, source_start_ms=1_241_100, keep_start_ms=1_241_500, keep_end_ms=2_000_000, raw_result_json=raw2),
        ]
        result, status = merge_segment_results(inputs)

        assert status == MERGE_STATUS_OK

        texts = [s["text_seg"] for s in result["stamp_sents"]]
        assert "第一段内容。" in texts
        assert "第二段内容。" in texts
        assert "第三段内容。" in texts
        assert "重叠尾" not in texts
        assert "重叠头" not in texts
        assert "重叠尾2" not in texts
        assert "重叠头2" not in texts

        starts = [s["ts"][0] for s in result["stamp_sents"]]
        for i in range(1, len(starts)):
            assert starts[i] >= starts[i - 1], f"Non-monotonic: {starts}"
