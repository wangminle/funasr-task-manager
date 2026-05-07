"""Task-group short commands for async agent architecture.

Each command is a fast, non-blocking step designed to let the calling agent
retain control between phases: scan → submit → status → download.
All commands output structured JSON (via --output json) for machine consumption.
"""

from __future__ import annotations

import json as _json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli import output as out
from cli.api_client import APIError
from cli.path_utils import detect_project_root, get_default_download_dir

app = typer.Typer()

SUPPORTED_EXTENSIONS = {
    ".wav", ".mp3", ".mp4", ".flac", ".ogg", ".webm",
    ".m4a", ".aac", ".wma", ".mkv", ".avi", ".mov", ".pcm",
}


def _probe_duration(path: Path) -> float | None:
    """Get media duration in seconds via ffprobe. Returns None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            info = _json.loads(result.stdout)
            return float(info.get("format", {}).get("duration", 0))
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, KeyError):
        pass
    return None


def _fingerprint(path: Path, stat: os.stat_result) -> str:
    """Build a fast fingerprint from path + size + mtime."""
    return f"{path}:{stat.st_size}:{stat.st_mtime_ns}"


def _resolve_manifest_item_path(item_path: str, source_dir: str) -> Path:
    """Resolve a manifest item path without double-prefixing source_dir."""
    path = Path(item_path)
    if path.is_absolute() or path.exists():
        return path

    if not source_dir:
        return path

    source = Path(source_dir)
    candidates: list[Path] = []

    try:
        relative_to_source = path.relative_to(source)
    except ValueError:
        relative_to_source = None

    if relative_to_source is not None:
        candidates.append(source / relative_to_source)

    path_parts = path.parts
    if source.name in path_parts:
        source_name_index = path_parts.index(source.name)
        remaining_parts = path_parts[source_name_index + 1:]
        if remaining_parts:
            candidates.append(source.joinpath(*remaining_parts))

    candidates.append(source / path)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


@app.command(name="scan")
def scan(
    ctx: typer.Context,
    source_dir: Path = typer.Argument(
        None,
        help="扫描目录；默认 runtime/agent-local-batch/inbox/",
    ),
    extensions: Optional[str] = typer.Option(
        None, "--extensions", "-e",
        help="限定扩展名（逗号分隔，如 .wav,.mp3）；默认全部支持格式",
    ),
    probe: bool = typer.Option(
        True, "--probe/--no-probe",
        help="是否用 ffprobe 探测时长（关闭可加速扫描）",
    ),
    chunk_size: int = typer.Option(
        50, "--chunk-size",
        help="每个提交块的文件数量",
    ),
):
    """扫描目录中的音视频文件，返回 JSON 清单。

    不上传、不提交——纯本地扫描，秒级返回。
    """
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if source_dir is None:
        source_dir = detect_project_root() / "runtime" / "agent-local-batch" / "inbox"
    source_dir = source_dir.resolve()

    if not source_dir.is_dir():
        out.error(f"目录不存在: {source_dir}")
        raise typer.Exit(1)

    allowed = SUPPORTED_EXTENSIONS
    if extensions:
        allowed = {ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
                   for ext in extensions.split(",")}

    items: list[dict] = []
    total_size = 0
    total_duration = 0.0
    scan_start = time.time()

    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in allowed:
            continue

        st = path.stat()
        duration = _probe_duration(path) if probe else None

        items.append({
            "index": len(items),
            "path": str(path.resolve()),
            "name": path.name,
            "size_bytes": st.st_size,
            "duration_sec": duration,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "fingerprint": _fingerprint(path, st),
        })
        total_size += st.st_size
        if duration:
            total_duration += duration

    scan_elapsed = round(time.time() - scan_start, 2)

    chunks = []
    for i in range(0, len(items), chunk_size):
        chunk_items = items[i:i + chunk_size]
        chunks.append({
            "chunk_index": len(chunks),
            "file_count": len(chunk_items),
            "start_index": i,
            "end_index": i + len(chunk_items) - 1,
        })

    result = {
        "source_dir": str(source_dir),
        "total_files": len(items),
        "total_size_bytes": total_size,
        "total_duration_sec": round(total_duration, 1),
        "chunk_size": chunk_size,
        "total_chunks": len(chunks),
        "scan_elapsed_sec": scan_elapsed,
        "chunks": chunks,
        "items": items,
    }

    out.render(
        c.output_format, data=result,
        title=f"扫描结果: {source_dir}",
        columns=["#", "文件名", "大小", "时长", "修改时间"],
        rows=[
            [
                it["index"],
                it["name"],
                f"{it['size_bytes'] / 1024 / 1024:.1f}MB",
                f"{it['duration_sec']:.0f}s" if it["duration_sec"] else "-",
                it["mtime"][:19],
            ]
            for it in items
        ],
        footer=(
            f"共 {len(items)} 个文件 · "
            f"{total_size / 1024 / 1024:.1f}MB · "
            f"时长 {total_duration:.0f}s · "
            f"{len(chunks)} 个块"
        ),
    )


@app.command(name="submit")
def submit(
    ctx: typer.Context,
    manifest_file: Optional[Path] = typer.Option(
        None, "--manifest", "-m",
        help="scan 输出的 JSON 文件路径；不指定时从 stdin 读取",
    ),
    language: str = typer.Option("auto", "--language", "-l", help="识别语言"),
    segment_level: str = typer.Option("10m", "--segment-level", help="切分策略: off/10m/20m/30m"),
    chunk_index: Optional[int] = typer.Option(
        None, "--chunk",
        help="只提交指定块（从 0 开始）；不指定则提交全部",
    ),
    hotwords: Optional[str] = typer.Option(None, "--hotwords", help="热词，逗号分隔"),
):
    """将 scan 结果提交到后端，返回 task_group_id。

    接收 scan 输出的 JSON（文件或 stdin），上传文件并创建任务。
    每个 chunk 产生一个独立的 task_group_id。秒级到分钟级返回（取决于文件大小）。
    """
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if manifest_file:
        manifest = _json.loads(manifest_file.read_text(encoding="utf-8"))
    else:
        import sys
        manifest = _json.loads(sys.stdin.read())

    all_items = manifest.get("items", [])
    chunks = manifest.get("chunks", [])
    cfg_chunk_size = manifest.get("chunk_size", 50)

    source_dir = manifest.get("source_dir", "")

    if chunk_index is not None:
        matching = [ch for ch in chunks if ch["chunk_index"] == chunk_index]
        if not matching:
            out.error(f"块 {chunk_index} 不存在（共 {len(chunks)} 块）")
            raise typer.Exit(1)
        ch = matching[0]
        selected_items = all_items[ch["start_index"]:ch["end_index"] + 1]
        chunk_indices = [chunk_index]
    else:
        selected_items = all_items
        if chunks:
            chunk_indices = [ch["chunk_index"] for ch in chunks]
        else:
            chunk_indices = list(range(max(1, -(-len(all_items) // cfg_chunk_size))))

    options = {}
    if hotwords:
        options["hotwords"] = hotwords

    submitted_groups: list[dict] = []
    upload_failures: list[dict] = []
    total_uploaded = 0
    submit_start = time.time()

    for ci in chunk_indices:
        if chunks:
            ch = next((c2 for c2 in chunks if c2["chunk_index"] == ci), None)
            if ch is None:
                out.error(f"块 {ci} 数据缺失")
                raise typer.Exit(1)
            chunk_items = all_items[ch["start_index"]:ch["end_index"] + 1]
        else:
            start = ci * cfg_chunk_size
            end = min(start + cfg_chunk_size, len(all_items))
            chunk_items = all_items[start:end]

        file_ids: list[str] = []
        file_map: dict[str, str] = {}

        for item in chunk_items:
            fpath = _resolve_manifest_item_path(item["path"], source_dir)
            if not fpath.exists():
                upload_failures.append({"name": item["name"], "error": "file not found"})
                continue
            try:
                file_data = c.client.upload_file(fpath)
                fid = file_data["file_id"]
                file_ids.append(fid)
                file_map[fid] = item["name"]
                total_uploaded += 1
                if not c.quiet:
                    out.info(f"  上传: {item['name']} → {fid}")
            except APIError as e:
                upload_failures.append({"name": item["name"], "error": e.detail})
                if not c.quiet:
                    out.error(f"  上传失败: {item['name']}: {e.detail}")

        if not file_ids:
            submitted_groups.append({
                "chunk_index": ci,
                "task_group_id": None,
                "task_count": 0,
                "error": "all uploads failed",
            })
            continue

        items_payload = [
            {"file_id": fid, "language": language, "options": options or None}
            for fid in file_ids
        ]
        try:
            tasks = c.client.create_tasks(items_payload, segment_level=segment_level)
            group_id = tasks[0].get("task_group_id") if tasks else None
            submitted_groups.append({
                "chunk_index": ci,
                "task_group_id": group_id,
                "task_count": len(tasks),
                "task_ids": [t["task_id"] for t in tasks],
            })
            if not c.quiet:
                out.success(f"  块 {ci}: 已创建 {len(tasks)} 个任务 (group: {group_id})")
        except APIError as e:
            submitted_groups.append({
                "chunk_index": ci,
                "task_group_id": None,
                "task_count": 0,
                "error": e.detail,
            })
            if not c.quiet:
                out.error(f"  块 {ci}: 创建任务失败: {e.detail}")

    submit_elapsed = round(time.time() - submit_start, 2)
    total_submitted = sum(g.get("task_count", 0) for g in submitted_groups)

    result = {
        "total_files": len(selected_items),
        "total_uploaded": total_uploaded,
        "total_submitted": total_submitted,
        "upload_failures": len(upload_failures),
        "submit_elapsed_sec": submit_elapsed,
        "groups": submitted_groups,
        "failures": upload_failures if upload_failures else [],
    }

    out.render(
        c.output_format, data=result,
        title="提交结果",
        columns=["块", "task_group_id", "任务数", "状态"],
        rows=[
            [
                g["chunk_index"],
                g.get("task_group_id") or "-",
                g.get("task_count", 0),
                "失败" if g.get("error") else "成功",
            ]
            for g in submitted_groups
        ],
        footer=(
            f"共上传 {result['total_uploaded']} 个文件 · "
            f"创建 {result['total_submitted']} 个任务 · "
            f"失败 {result['upload_failures']} · 耗时 {submit_elapsed}s"
        ),
    )

    if result["total_submitted"] == 0:
        raise typer.Exit(1)


@app.command(name="status")
def status(
    ctx: typer.Context,
    group_id: str = typer.Argument(..., help="任务组 ID（task_group_id）"),
):
    """查询任务组状态，返回 JSON 进度。

    单次查询，秒级返回——不做轮询。供子 Agent 定时调用。
    """
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        stats = c.client.get_task_group(group_id)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    total = stats.get("total", 0)
    succeeded = stats.get("succeeded", 0)
    failed = stats.get("failed", 0)
    canceled = stats.get("canceled", 0)
    in_progress = stats.get("in_progress", 0)

    result = {
        "task_group_id": group_id,
        "status": "COMPLETED" if stats.get("is_complete") else "RUNNING",
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "canceled": canceled,
        "in_progress": in_progress,
        "progress": stats.get("progress", 0),
        "is_complete": stats.get("is_complete", False),
    }

    out.render(
        c.output_format, data=result,
        title=f"任务组状态: {group_id}",
        columns=["字段", "值"],
        rows=[
            ["状态", result["status"]],
            ["总数", str(total)],
            ["成功", str(succeeded)],
            ["失败", str(failed)],
            ["取消", str(canceled)],
            ["进行中", str(in_progress)],
            ["进度", f"{result['progress'] * 100:.0f}%"],
        ],
    )


@app.command(name="download")
def download(
    ctx: typer.Context,
    group_id: str = typer.Argument(..., help="任务组 ID（task_group_id）"),
    fmt: str = typer.Option(
        "txt", "--format", "-f",
        help="结果格式: json/txt/srt（逗号分隔可选多格式）",
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-d",
        help="结果下载目录（默认 runtime/storage/downloads）",
    ),
):
    """下载任务组中已完成任务的结果文件。

    只下载 SUCCEEDED 状态的任务结果，返回下载摘要 JSON。
    """
    from cli.main import get_ctx
    from cli.commands.task import _result_output_filename
    c = get_ctx(ctx)

    resolved_dir = output_dir or get_default_download_dir()
    formats = [f.strip() for f in fmt.split(",") if f.strip()]
    if not formats:
        formats = ["txt"]

    try:
        stats = c.client.get_task_group(group_id)
        group_data = c.client.list_group_tasks(group_id, page_size=500)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    tasks = group_data.get("items", [])
    succeeded = [t for t in tasks if t["status"] == "SUCCEEDED"]

    if not succeeded:
        result = {
            "task_group_id": group_id,
            "downloaded": 0,
            "total_succeeded": 0,
            "message": "没有已完成的任务可下载",
        }
        out.render(c.output_format, data=result)
        raise typer.Exit(1)

    resolved_dir.mkdir(parents=True, exist_ok=True)
    downloaded: list[dict] = []
    download_failures: list[dict] = []
    seen_names: dict[str, int] = {}

    for t in succeeded:
        tid = t["task_id"]
        outputs: dict[str, str] = {}
        for f in formats:
            try:
                content = c.client.get_result(tid, fmt=f)
                result_name = _result_output_filename(t.get("file_name"), tid, f, seen_names)
                dest = resolved_dir / result_name
                dest.write_text(content, encoding="utf-8")
                outputs[f] = str(dest)
            except APIError as e:
                download_failures.append({
                    "task_id": tid,
                    "format": f,
                    "error": e.detail,
                })
        if outputs:
            downloaded.append({
                "task_id": tid,
                "file_name": t.get("file_name", ""),
                "outputs": outputs,
            })

    result = {
        "task_group_id": group_id,
        "output_dir": str(resolved_dir),
        "formats": formats,
        "total_succeeded": len(succeeded),
        "downloaded": len(downloaded),
        "download_failures": len(download_failures),
        "files": downloaded,
        "failures": download_failures if download_failures else [],
    }

    out.render(
        c.output_format, data=result,
        title=f"下载结果: {group_id}",
        columns=["task_id", "文件名", "输出"],
        rows=[
            [
                d["task_id"][:12] + "...",
                d.get("file_name", ""),
                ", ".join(d["outputs"].values()),
            ]
            for d in downloaded
        ],
        footer=f"已下载 {len(downloaded)} 个 · 失败 {len(download_failures)} 个 · 目录: {resolved_dir}",
    )

    if not downloaded:
        raise typer.Exit(1)
