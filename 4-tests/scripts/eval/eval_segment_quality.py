#!/usr/bin/env python3
"""VAD 切分前后输出质量对比评估脚本。

使用方式:
    python eval_segment_quality.py --base-url http://localhost:8000 [--audio-dir <path>]

流程:
    1. 对每个长音频文件分别做两组转写：
       - 基准组：segment_level=off，整文件直接转写
       - 实验组：segment_level=10m，VAD 切分后并行转写再合并
    2. 对比维度：
       - 文本完整性：字数差异比例
       - 切分边界质量：切分点附近重复/缺失检测
       - 时间戳连续性：单调递增检查、跳变检测
       - 整体相似度：字符级编辑距离比
    3. 输出评估报告（Markdown）

依赖: 只使用标准库 + 项目已有的 cli.api_client
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
BACKEND_SRC = PROJECT_ROOT / "3-dev" / "src" / "backend"
sys.path.insert(0, str(BACKEND_SRC))

from cli.api_client import ASRClient, APIError  # noqa: E402

DEFAULT_AUDIO_DIR = PROJECT_ROOT / "4-tests" / "batch-testing" / "assets" / "3-长音频"
LONG_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".mp4", ".wav", ".flac", ".ogg"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_audio_files(audio_dir: Path) -> list[Path]:
    files = sorted(
        p for p in audio_dir.iterdir()
        if p.suffix.lower() in LONG_AUDIO_EXTENSIONS and p.is_file()
    )
    return files


def _upload_and_transcribe(
    client: ASRClient, audio_path: Path, segment_level: str, poll_interval: float = 5.0,
    timeout: float = 3600.0,
) -> dict:
    """Upload → create task → poll until done → return result JSON + task info."""
    print(f"  上传 {audio_path.name} ...")
    file_info = client.upload_file(audio_path)
    file_id = file_info["file_id"]

    tasks = client.create_tasks(
        [{"file_id": file_id, "language": "auto"}],
        segment_level=segment_level,
    )
    task = tasks[0]
    task_id = task["task_id"]
    print(f"  任务已创建 task_id={task_id}  segment_level={segment_level}")

    start = time.time()
    while time.time() - start < timeout:
        info = client.get_task(task_id)
        status = info["status"]
        if status == "SUCCEEDED":
            result_json = client.get_result(task_id, fmt="json")
            result_txt = client.get_result(task_id, fmt="txt")
            return {
                "task": info,
                "result_json": result_json,
                "result_txt": result_txt,
            }
        if status in ("FAILED", "CANCELED"):
            return {
                "task": info,
                "result_json": None,
                "result_txt": None,
                "error": info.get("error_message", status),
            }
        time.sleep(poll_interval)

    return {"task": {"task_id": task_id}, "result_json": None, "result_txt": None, "error": "TIMEOUT"}


def _parse_stamp_sents(raw_json: str | None) -> list[dict]:
    """Extract timestamped sentences from result JSON.

    Handles both the formatted output (``segments`` with ``start_ms``/
    ``end_ms``) and the raw FunASR output (``stamp_sents`` with ``ts``).
    Returns a normalised list where each dict has ``start_ms`` and ``end_ms``.
    """
    if not raw_json:
        return []
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return []

    formatted_segs = data.get("segments")
    if formatted_segs and isinstance(formatted_segs, list):
        return [
            {"start_ms": s.get("start_ms", 0), "end_ms": s.get("end_ms", 0), "text": s.get("text", "")}
            for s in formatted_segs
            if isinstance(s, dict)
        ]

    stamp_sents = data.get("stamp_sents")
    if stamp_sents and isinstance(stamp_sents, list):
        result = []
        for sent in stamp_sents:
            if not isinstance(sent, dict):
                continue
            ts = sent.get("ts")
            if ts and isinstance(ts, list) and len(ts) >= 2:
                result.append({"start_ms": ts[0], "end_ms": ts[1], "text": sent.get("text_seg", "")})
        return result

    return []


def _extract_text(raw_json: str | None) -> str:
    if not raw_json:
        return ""
    try:
        data = json.loads(raw_json)
        return data.get("text", "")
    except (json.JSONDecodeError, TypeError):
        return ""


def _char_edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein distance between two strings (character-level)."""
    if len(a) > 5000 or len(b) > 5000:
        sm = difflib.SequenceMatcher(None, a, b)
        matching = sum(block.size for block in sm.get_matching_blocks())
        return max(len(a), len(b)) - matching
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            if a[i - 1] == b[j - 1]:
                dp[j] = prev
            else:
                dp[j] = 1 + min(prev, dp[j], dp[j - 1])
            prev = temp
    return dp[n]


def _check_timestamp_monotonicity(stamp_sents: list[dict]) -> dict:
    """Check if timestamps are monotonically increasing, report violations.

    Expects normalised dicts with ``start_ms`` and ``end_ms`` keys
    (as returned by ``_parse_stamp_sents``).
    """
    violations = []
    prev_end = -1
    for i, sent in enumerate(stamp_sents):
        start = sent.get("start_ms", 0)
        end = sent.get("end_ms", 0)
        if start == 0 and end == 0:
            continue
        if start < prev_end:
            violations.append({
                "index": i,
                "prev_end_ms": prev_end,
                "cur_start_ms": start,
                "gap_ms": start - prev_end,
            })
        prev_end = max(prev_end, end)

    return {
        "total_sentences": len(stamp_sents),
        "violation_count": len(violations),
        "violations": violations[:10],
    }


def _find_boundary_issues(baseline_txt: str, experiment_txt: str) -> list[str]:
    """Detect potential boundary issues by comparing baseline and experiment texts.

    Checks for:
    1. Consecutive duplicate Chinese phrases in experiment text (anywhere).
    2. Local insertions/deletions between baseline and experiment texts near
       positions that differ — these likely indicate cut-point artifacts.
    """
    issues: list[str] = []

    chars_exp = re.findall(r'[\u4e00-\u9fff]+', experiment_txt)
    for i in range(len(chars_exp) - 1):
        if chars_exp[i] == chars_exp[i + 1] and len(chars_exp[i]) >= 2:
            issues.append(f"可能重复: '{chars_exp[i]}' 在位置 {i}")

    base_chars = list(baseline_txt.replace(" ", ""))
    exp_chars = list(experiment_txt.replace(" ", ""))
    min_len = min(len(base_chars), len(exp_chars))
    diff_positions: list[int] = []
    for i in range(min_len):
        if base_chars[i] != exp_chars[i]:
            diff_positions.append(i)
    if diff_positions:
        ctx = 5
        for pos in diff_positions[:10]:
            base_ctx = "".join(base_chars[max(0, pos - ctx): pos + ctx + 1])
            exp_ctx = "".join(exp_chars[max(0, pos - ctx): pos + ctx + 1])
            issues.append(f"位置{pos}附近差异: baseline='{base_ctx}' vs experiment='{exp_ctx}'")

    if len(issues) > 20:
        issues = issues[:20] + [f"... 共 {len(issues)} 处"]
    return issues


# ---------------------------------------------------------------------------
# Evaluation per file
# ---------------------------------------------------------------------------


def _evaluate_single(
    client: ASRClient, audio_path: Path, poll_interval: float, timeout: float,
) -> dict:
    """Run baseline + experiment for one audio file, compute metrics."""
    print(f"\n{'='*60}")
    print(f"评估文件: {audio_path.name}")
    print(f"{'='*60}")

    print("\n[基准组] segment_level=off")
    baseline = _upload_and_transcribe(client, audio_path, "off", poll_interval, timeout)
    if baseline.get("error"):
        print(f"  ❌ 基准组失败: {baseline['error']}")
        return {"file": audio_path.name, "error": f"baseline failed: {baseline['error']}"}

    print("\n[实验组] segment_level=10m")
    experiment = _upload_and_transcribe(client, audio_path, "10m", poll_interval, timeout)
    if experiment.get("error"):
        print(f"  ❌ 实验组失败: {experiment['error']}")
        return {"file": audio_path.name, "error": f"experiment failed: {experiment['error']}"}

    bl_text = _extract_text(baseline["result_json"])
    ex_text = _extract_text(experiment["result_json"])
    bl_txt = baseline["result_txt"] or ""
    ex_txt = experiment["result_txt"] or ""

    bl_chars = len(re.sub(r'\s+', '', bl_text))
    ex_chars = len(re.sub(r'\s+', '', ex_text))
    char_diff_ratio = abs(bl_chars - ex_chars) / max(bl_chars, 1)

    edit_dist = _char_edit_distance(
        re.sub(r'\s+', '', bl_text), re.sub(r'\s+', '', ex_text)
    )
    similarity = 1.0 - edit_dist / max(bl_chars, ex_chars, 1)

    sm = difflib.SequenceMatcher(None, bl_txt, ex_txt)
    seq_ratio = sm.ratio()

    bl_stamps = _parse_stamp_sents(baseline["result_json"])
    ex_stamps = _parse_stamp_sents(experiment["result_json"])
    bl_ts_check = _check_timestamp_monotonicity(bl_stamps)
    ex_ts_check = _check_timestamp_monotonicity(ex_stamps)

    boundary_issues = _find_boundary_issues(bl_txt, ex_txt)

    bl_segments = baseline["task"].get("segments")
    ex_segments = experiment["task"].get("segments")

    metrics = {
        "file": audio_path.name,
        "baseline_task_id": baseline["task"]["task_id"],
        "experiment_task_id": experiment["task"]["task_id"],
        "baseline_chars": bl_chars,
        "experiment_chars": ex_chars,
        "char_diff_ratio": round(char_diff_ratio, 4),
        "char_edit_distance": edit_dist,
        "char_similarity": round(similarity, 4),
        "sequence_match_ratio": round(seq_ratio, 4),
        "baseline_ts_violations": bl_ts_check["violation_count"],
        "experiment_ts_violations": ex_ts_check["violation_count"],
        "experiment_ts_details": ex_ts_check["violations"],
        "boundary_issues": boundary_issues,
        "baseline_segments": bl_segments,
        "experiment_segments": ex_segments,
    }

    print(f"\n  基准组字数: {bl_chars}  实验组字数: {ex_chars}  差异率: {char_diff_ratio:.2%}")
    print(f"  字符相似度: {similarity:.4f}  序列匹配率: {seq_ratio:.4f}")
    print(f"  基准组时间戳违规: {bl_ts_check['violation_count']}  实验组: {ex_ts_check['violation_count']}")
    if boundary_issues:
        print(f"  边界问题: {len(boundary_issues)} 处")
    if ex_segments:
        print(f"  实验组分段: {ex_segments}")

    return metrics


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _generate_report(results: list[dict], output_path: Path) -> None:
    """Generate a Markdown evaluation report."""
    today = datetime.now().strftime("%Y%m%d")
    lines = [
        f"# VAD 切分质量评估报告 - {today}",
        "",
        "## 评估概要",
        "",
        f"- 评估文件数: {len(results)}",
        f"- 评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 基准组: `segment_level=off`（整文件直接转写）",
        f"- 实验组: `segment_level=10m`（VAD 切分并行转写 + 合并）",
        "",
        "## 评估结果",
        "",
    ]

    for r in results:
        lines.append(f"### {r['file']}")
        lines.append("")

        if r.get("error"):
            lines.append(f"**错误**: {r['error']}")
            lines.append("")
            continue

        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 基准组 task_id | `{r['baseline_task_id']}` |")
        lines.append(f"| 实验组 task_id | `{r['experiment_task_id']}` |")
        lines.append(f"| 基准组字数 | {r['baseline_chars']} |")
        lines.append(f"| 实验组字数 | {r['experiment_chars']} |")
        lines.append(f"| 字数差异率 | {r['char_diff_ratio']:.2%} |")
        lines.append(f"| 字符相似度 | {r['char_similarity']:.4f} |")
        lines.append(f"| 序列匹配率 | {r['sequence_match_ratio']:.4f} |")
        lines.append(f"| 字符编辑距离 | {r['char_edit_distance']} |")
        lines.append(f"| 基准组时间戳违规 | {r['baseline_ts_violations']} |")
        lines.append(f"| 实验组时间戳违规 | {r['experiment_ts_violations']} |")
        lines.append("")

        if r.get("experiment_segments"):
            seg = r["experiment_segments"]
            lines.append(f"**实验组分段信息**: 共 {seg.get('total', '?')} 段, "
                         f"成功 {seg.get('succeeded', '?')}, 失败 {seg.get('failed', '?')}")
            lines.append("")

        if r.get("experiment_ts_details"):
            lines.append("**时间戳违规详情** (前 10 条):")
            lines.append("")
            for v in r["experiment_ts_details"]:
                lines.append(f"- 句 #{v['index']}: 前句结束 {v['prev_end_ms']}ms, "
                             f"当前开始 {v['cur_start_ms']}ms, 间隔 {v['gap_ms']}ms")
            lines.append("")

        if r.get("boundary_issues"):
            lines.append("**切分边界问题**:")
            lines.append("")
            for issue in r["boundary_issues"]:
                lines.append(f"- {issue}")
            lines.append("")

    lines.append("## 评估结论")
    lines.append("")

    ok_count = sum(1 for r in results if not r.get("error"))
    if ok_count == 0:
        lines.append("所有文件评估失败，无法得出结论。")
    else:
        sims = [r["char_similarity"] for r in results if not r.get("error")]
        avg_sim = sum(sims) / len(sims)
        diffs = [r["char_diff_ratio"] for r in results if not r.get("error")]
        avg_diff = sum(diffs) / len(diffs)
        ts_issues = [r["experiment_ts_violations"] for r in results if not r.get("error")]

        lines.append(f"- 平均字符相似度: **{avg_sim:.4f}**")
        lines.append(f"- 平均字数差异率: **{avg_diff:.2%}**")
        lines.append(f"- 实验组时间戳违规总数: **{sum(ts_issues)}**")
        lines.append("")

        if avg_sim >= 0.98 and avg_diff <= 0.02 and sum(ts_issues) == 0:
            lines.append("**结论: VAD 切分对转写质量无显著影响，质量保持一致。** ✅")
        elif avg_sim >= 0.95:
            lines.append("**结论: VAD 切分对转写质量影响较小，基本可接受。** ⚠️")
        else:
            lines.append("**结论: VAD 切分对转写质量有明显影响，需要进一步调优。** ❌")

    lines.append("")

    report_text = "\n".join(lines)
    output_path.write_text(report_text, encoding="utf-8")
    print(f"\n报告已保存: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="VAD 切分前后转写质量对比评估")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API 地址")
    parser.add_argument("--api-key", default=None, help="API Key")
    parser.add_argument(
        "--audio-dir",
        type=Path,
        default=DEFAULT_AUDIO_DIR,
        help="长音频文件目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="评估报告输出路径（默认: 4-tests/batch-testing/outputs/benchmark/）",
    )
    parser.add_argument("--poll-interval", type=float, default=5.0, help="轮询间隔(秒)")
    parser.add_argument("--timeout", type=float, default=3600.0, help="单任务超时(秒)")
    args = parser.parse_args()

    audio_files = _find_audio_files(args.audio_dir)
    if not audio_files:
        print(f"在 {args.audio_dir} 中未找到音频文件")
        sys.exit(1)

    print(f"找到 {len(audio_files)} 个音频文件:")
    for f in audio_files:
        print(f"  - {f.name}")

    client = ASRClient(base_url=args.base_url, api_key=args.api_key, timeout=args.timeout)

    try:
        health = client.health()
        print(f"\n服务状态: {health.get('status', 'unknown')}")
    except Exception as e:
        print(f"无法连接到服务: {e}")
        sys.exit(1)

    results = []
    for audio_path in audio_files:
        try:
            metrics = _evaluate_single(client, audio_path, args.poll_interval, args.timeout)
            results.append(metrics)
        except Exception as e:
            print(f"\n评估 {audio_path.name} 时出错: {e}")
            results.append({"file": audio_path.name, "error": str(e)})

    if args.output:
        output_path = args.output
    else:
        today = datetime.now().strftime("%Y%m%d")
        output_dir = PROJECT_ROOT / "4-tests" / "batch-testing" / "outputs" / "benchmark"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"vad-segment-quality-eval-{today}.md"

    _generate_report(results, output_path)

    json_path = output_path.with_suffix(".json")
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"原始数据已保存: {json_path}")

    client.close()


if __name__ == "__main__":
    main()
