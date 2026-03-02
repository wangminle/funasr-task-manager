"""Result formatting: JSON, TXT, SRT."""

import json
from dataclasses import dataclass


@dataclass
class TimestampSegment:
    start_ms: int
    end_ms: int
    text: str


def parse_timestamp_segments(raw_result: dict) -> list[TimestampSegment]:
    """Extract timestamp segments from ASR result."""
    segments: list[TimestampSegment] = []

    stamp_sents = raw_result.get("stamp_sents")
    if stamp_sents and isinstance(stamp_sents, list):
        for sent in stamp_sents:
            if not isinstance(sent, dict):
                continue
            text_seg = sent.get("text_seg", "")
            punc = sent.get("punc", "")
            ts = sent.get("ts")
            if ts and isinstance(ts, list) and len(ts) >= 2:
                segments.append(TimestampSegment(
                    start_ms=int(ts[0]),
                    end_ms=int(ts[1]),
                    text=text_seg + punc,
                ))
        return segments

    timestamp = raw_result.get("timestamp")
    text = raw_result.get("text", "")
    if timestamp and isinstance(timestamp, list) and text:
        for i, ts_pair in enumerate(timestamp):
            if isinstance(ts_pair, list) and len(ts_pair) >= 2:
                segments.append(TimestampSegment(
                    start_ms=int(ts_pair[0]),
                    end_ms=int(ts_pair[1]),
                    text=str(text[i]) if i < len(text) else "",
                ))

    if not segments and text:
        segments.append(TimestampSegment(start_ms=0, end_ms=0, text=text))

    return segments


def format_ms_to_srt_time(ms: int) -> str:
    """Convert milliseconds to SRT time format: HH:MM:SS,mmm"""
    hours = ms // 3600000
    minutes = (ms % 3600000) // 60000
    seconds = (ms % 60000) // 1000
    millis = ms % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def to_json(raw_result: dict, pretty: bool = True) -> str:
    """Format result as JSON."""
    output = {
        "text": raw_result.get("text", ""),
        "mode": raw_result.get("mode", ""),
        "segments": [],
    }
    for seg in parse_timestamp_segments(raw_result):
        output["segments"].append({
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "text": seg.text,
        })
    indent = 2 if pretty else None
    return json.dumps(output, ensure_ascii=False, indent=indent)


def to_txt(raw_result: dict) -> str:
    """Format result as plain text."""
    text = raw_result.get("text", "")
    if text:
        return text

    segments = parse_timestamp_segments(raw_result)
    return "".join(seg.text for seg in segments)


def to_srt(raw_result: dict) -> str:
    """Format result as SRT subtitle format."""
    segments = parse_timestamp_segments(raw_result)
    if not segments:
        text = raw_result.get("text", "")
        if text:
            return f"1\n00:00:00,000 --> 00:00:00,000\n{text}\n"
        return ""

    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        start = format_ms_to_srt_time(seg.start_ms)
        end = format_ms_to_srt_time(seg.end_ms)
        lines.append(f"{i}")
        lines.append(f"{start} --> {end}")
        lines.append(seg.text)
        lines.append("")

    return "\n".join(lines)
