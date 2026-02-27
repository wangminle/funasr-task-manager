"""Result formatter unit tests - JSON, TXT, SRT."""

import json

import pytest

from app.services.result_formatter import (
    TimestampSegment, format_ms_to_srt_time,
    parse_timestamp_segments, to_json, to_txt, to_srt,
)


@pytest.mark.unit
class TestFormatMsToSrtTime:
    def test_zero(self):
        assert format_ms_to_srt_time(0) == "00:00:00,000"

    def test_normal(self):
        assert format_ms_to_srt_time(3661500) == "01:01:01,500"

    def test_large(self):
        assert format_ms_to_srt_time(7200000) == "02:00:00,000"


@pytest.mark.unit
class TestParseTimestampSegments:
    def test_stamp_sents_format(self):
        raw = {"stamp_sents": [
            {"text_seg": "你好", "punc": "，", "ts": [0, 500]},
            {"text_seg": "世界", "punc": "。", "ts": [500, 1000]},
        ]}
        segs = parse_timestamp_segments(raw)
        assert len(segs) == 2
        assert segs[0].text == "你好，"
        assert segs[0].start_ms == 0
        assert segs[0].end_ms == 500

    def test_plain_text_fallback(self):
        raw = {"text": "你好世界"}
        segs = parse_timestamp_segments(raw)
        assert len(segs) == 1
        assert segs[0].text == "你好世界"

    def test_empty_result(self):
        raw = {}
        segs = parse_timestamp_segments(raw)
        assert segs == []


@pytest.mark.unit
class TestToJson:
    def test_json_output(self):
        raw = {"text": "测试结果", "mode": "offline"}
        result = to_json(raw)
        parsed = json.loads(result)
        assert parsed["text"] == "测试结果"
        assert parsed["mode"] == "offline"


@pytest.mark.unit
class TestToTxt:
    def test_plain_text(self):
        raw = {"text": "你好世界"}
        assert to_txt(raw) == "你好世界"

    def test_from_segments(self):
        raw = {"stamp_sents": [{"text_seg": "你好", "punc": "", "ts": [0, 500]}]}
        assert to_txt(raw) == "你好"


@pytest.mark.unit
class TestToSrt:
    def test_srt_with_timestamps(self):
        raw = {"stamp_sents": [
            {"text_seg": "第一句", "punc": "。", "ts": [0, 2000]},
            {"text_seg": "第二句", "punc": "。", "ts": [2000, 4000]},
        ]}
        srt = to_srt(raw)
        assert "1\n" in srt
        assert "00:00:00,000 --> 00:00:02,000" in srt
        assert "第一句。" in srt
        assert "2\n" in srt

    def test_srt_plain_text_fallback(self):
        raw = {"text": "无时间戳"}
        srt = to_srt(raw)
        assert "无时间戳" in srt

    def test_srt_empty(self):
        raw = {}
        assert to_srt(raw) == ""
