"""Merge multiple segment ASR results into a single unified result.

Used by the VAD parallel transcription pipeline to combine per-segment
FunASR outputs back into one coherent transcript with globally consistent
timestamps.  The merged result dict is compatible with the existing
``to_json / to_txt / to_srt`` formatters in ``result_formatter.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.services.result_formatter import TimestampSegment, parse_timestamp_segments
from app.observability.logging import get_logger

logger = get_logger(__name__)

MERGE_STATUS_OK = "OK"
MERGE_STATUS_TEXT_ONLY_FALLBACK = "TEXT_ONLY_FALLBACK"


@dataclass
class SegmentInput:
    """Lightweight DTO decoupling the merger from the ORM model."""
    segment_index: int
    source_start_ms: int
    keep_start_ms: int
    keep_end_ms: int
    raw_result_json: str


def _filter_and_offset(
    parsed: list[TimestampSegment],
    source_start_ms: int,
    keep_start_ms: int,
    keep_end_ms: int,
) -> list[TimestampSegment]:
    """Shift timestamps to the global timeline and keep only those whose
    start falls within the ``[keep_start, keep_end)`` interval.

    This ensures each sentence is emitted exactly once even when adjacent
    segments share an overlap region.
    """
    result: list[TimestampSegment] = []
    for seg in parsed:
        global_start = seg.start_ms + source_start_ms
        global_end = seg.end_ms + source_start_ms
        if keep_start_ms <= global_start < keep_end_ms:
            result.append(TimestampSegment(
                start_ms=global_start,
                end_ms=global_end,
                text=seg.text,
            ))
    return result


def merge_segment_results(
    segments: list[SegmentInput],
) -> tuple[dict, str]:
    """Merge per-segment ASR results into one unified raw-result dict.

    Returns ``(raw_result_dict, merge_status)`` where *merge_status* is
    :data:`MERGE_STATUS_OK` or :data:`MERGE_STATUS_TEXT_ONLY_FALLBACK`
    (at least one segment had no usable timestamps).

    The returned dict contains ``text``, ``stamp_sents``, and ``mode``
    keys ready for ``to_json / to_txt / to_srt``.
    """
    if not segments:
        return {"text": "", "stamp_sents": [], "mode": ""}, MERGE_STATUS_OK

    sorted_segs = sorted(segments, key=lambda s: s.segment_index)

    all_ts: list[TimestampSegment] = []
    all_text_parts: list[str] = []
    has_text_only_fallback = False
    mode = ""

    for seg_input in sorted_segs:
        try:
            raw = json.loads(seg_input.raw_result_json) if seg_input.raw_result_json else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("segment_result_parse_error", idx=seg_input.segment_index)
            raw = {}

        if not mode and raw.get("mode"):
            mode = raw["mode"]

        parsed = parse_timestamp_segments(raw)

        has_real_ts = any(ts.start_ms > 0 or ts.end_ms > 0 for ts in parsed)

        if has_real_ts:
            filtered = _filter_and_offset(
                parsed,
                source_start_ms=seg_input.source_start_ms,
                keep_start_ms=seg_input.keep_start_ms,
                keep_end_ms=seg_input.keep_end_ms,
            )
            all_ts.extend(filtered)
            all_text_parts.append("".join(ts.text for ts in filtered))
        else:
            text = raw.get("text", "")
            if text:
                all_text_parts.append(text)
                has_text_only_fallback = True

    merged_text = "".join(all_text_parts)

    stamp_sents = [
        {"text_seg": ts.text, "punc": "", "text": ts.text, "ts": [ts.start_ms, ts.end_ms]}
        for ts in all_ts
    ]

    result = {
        "text": merged_text,
        "stamp_sents": stamp_sents,
        "mode": mode,
    }

    merge_status = (
        MERGE_STATUS_TEXT_ONLY_FALLBACK if has_text_only_fallback
        else MERGE_STATUS_OK
    )

    logger.info(
        "segment_results_merged",
        segment_count=len(sorted_segs),
        output_sentences=len(stamp_sents),
        text_length=len(merged_text),
        status=merge_status,
    )

    return result, merge_status
