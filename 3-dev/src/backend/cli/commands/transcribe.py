"""Transcribe command - supports both single-file and batch parallel modes.

Single file:  upload → create → wait → download  (backward compatible)
Batch mode:   upload all → batch create → poll batch progress → download all
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer

from cli.api_client import APIError
from cli import output as out
from cli.path_utils import get_default_download_dir


def transcribe(
    ctx: typer.Context,
    files: list[Path] = typer.Argument(..., help="要转写的文件路径（支持多个）"),
    language: str = typer.Option("auto", "--language", "-l", help="识别语言"),
    hotwords: Optional[str] = typer.Option(None, "--hotwords", help="热词，逗号分隔"),
    fmt: str = typer.Option("json", "--format", "-f", help="结果格式: json/txt/srt"),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        "-d",
        help="结果下载目录（默认写入仓库根目录 runtime/storage/downloads）",
    ),
    save: Optional[Path] = typer.Option(None, "--save", help="保存到指定文件（单文件时）"),
    callback: Optional[str] = typer.Option(None, "--callback", help="回调地址"),
    no_wait: bool = typer.Option(False, "--no-wait", help="不等待完成"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="轮询间隔(秒)"),
    wait_timeout: float = typer.Option(3600.0, "--timeout", help="超时(秒)"),
    batch: bool = typer.Option(False, "--batch", help="强制批量模式（多文件时自动启用）"),
    download: bool = typer.Option(True, "--download/--no-download", help="完成后自动下载结果"),
    json_summary: bool = typer.Option(False, "--json-summary", help="输出批次 JSON 摘要"),
    auto_segment: str = typer.Option("auto", "--auto-segment", help="VAD 切分策略: auto/on/off"),
):
    """一键转写：上传 → 创建任务 → 等待完成 → 下载结果。

    多文件时自动使用批量模式，一次性创建所有任务，由后端并行调度到多台服务器。
    """
    from cli.main import get_ctx
    c = get_ctx(ctx)

    existing = [f for f in files if f.exists()]
    if not existing:
        out.error("没有找到有效的文件")
        raise typer.Exit(1)

    use_batch = batch or len(existing) > 1
    resolved_output_dir = output_dir or get_default_download_dir()

    if use_batch:
        _run_batch(c, existing, language, hotwords, fmt, resolved_output_dir, callback,
                   no_wait, poll_interval, wait_timeout, download, json_summary, auto_segment)
    else:
        _run_single(c, existing[0], language, hotwords, fmt, resolved_output_dir, save,
                    callback, no_wait, poll_interval, wait_timeout, auto_segment)


def _run_single(c, fp, language, hotwords, fmt, output_dir, save,
                callback, no_wait, poll_interval, wait_timeout, auto_segment="auto"):
    """Original single-file flow: upload → create → wait → download."""
    from cli.progress import wait_for_task

    options = {}
    if hotwords:
        options["hotwords"] = hotwords

    if not c.quiet:
        out.info(f"[1/4] 上传: {fp.name}")
    try:
        file_data = c.client.upload_file(fp)
    except APIError as e:
        out.error(f"上传失败 {fp.name}: {e.detail}")
        raise typer.Exit(1)

    if not c.quiet:
        out.info(f"[2/4] 创建任务: {file_data['file_id']}")
    items = [{"file_id": file_data["file_id"], "language": language, "options": options or None}]
    cb = {"url": callback} if callback else None
    try:
        tasks = c.client.create_tasks(items, callback=cb, auto_segment=auto_segment)
    except APIError as e:
        out.error(f"创建任务失败: {e.detail}")
        raise typer.Exit(1)

    task = tasks[0]
    task_id = task["task_id"]

    if no_wait:
        out.render(c.output_format, data=[{"file": fp.name, "task_id": task_id, "status": task["status"]}],
                   title="任务已提交", columns=["文件", "task_id", "状态"],
                   rows=[[fp.name, task_id[:12] + "...", task["status"]]])
        return

    if not c.quiet:
        out.info(f"[3/4] 等待完成: {task_id}")
    try:
        task = wait_for_task(c.client, task_id, poll_interval=poll_interval,
                             timeout=wait_timeout, quiet=c.quiet)
    except TimeoutError:
        out.error(f"任务超时: {task_id}")
        raise typer.Exit(1)

    if task["status"] != "SUCCEEDED":
        out.error(f"任务失败: {task_id} [{task['status']}] {task.get('error_message', '')}")
        raise typer.Exit(1)

    if not c.quiet:
        out.info(f"[4/4] 下载结果: {task_id}")
    try:
        content = c.client.get_result(task_id, fmt=fmt)
    except APIError as e:
        out.error(f"下载结果失败: {e.detail}")
        raise typer.Exit(1)

    if save:
        dest = save
    else:
        suffix = {"json": ".json", "txt": ".txt", "srt": ".srt"}.get(fmt, ".json")
        dest = output_dir / f"{fp.stem}_result{suffix}"

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")

    if not c.quiet:
        out.success(f"完成: {fp.name} → {dest}")
    out.render(c.output_format,
               data=[{"file": fp.name, "task_id": task_id, "status": "SUCCEEDED", "output": str(dest)}],
               title="转写结果", columns=["文件", "task_id", "状态", "输出"],
               rows=[[fp.name, task_id[:12] + "...", "SUCCEEDED", str(dest)]])


def _run_batch(c, files, language, hotwords, fmt, output_dir, callback,
               no_wait, poll_interval, wait_timeout, download_results, json_summary,
               auto_segment="auto"):
    """Batch mode: upload all → batch create → poll batch → download all."""
    import json

    options = {}
    if hotwords:
        options["hotwords"] = hotwords

    total = len(files)

    # Phase 1: Upload all files
    if not c.quiet:
        out.info(f"[1/4] 批量上传 {total} 个文件...")
    upload_map: list[tuple[Path, str]] = []
    upload_failures: list[str] = []
    for i, fp in enumerate(files, 1):
        try:
            file_data = c.client.upload_file(fp)
            upload_map.append((fp, file_data["file_id"]))
            if not c.quiet:
                out.info(f"  上传 ({i}/{total}): {fp.name} → {file_data['file_id']}")
        except APIError as e:
            upload_failures.append(fp.name)
            out.error(f"  上传失败 {fp.name}: {e.detail}")

    if not upload_map:
        out.error("所有文件上传失败")
        raise typer.Exit(1)

    if upload_failures and not c.quiet:
        out.error(f"  {len(upload_failures)}/{total} 个文件上传失败: {', '.join(upload_failures)}")

    # Phase 2: Batch create tasks
    if not c.quiet:
        out.info(f"[2/4] 批量创建 {len(upload_map)} 个任务...")
    items = [{"file_id": fid, "language": language, "options": options or None}
             for _, fid in upload_map]
    cb = {"url": callback} if callback else None
    try:
        tasks = c.client.create_tasks(items, callback=cb, auto_segment=auto_segment)
    except APIError as e:
        out.error(f"批量创建任务失败: {e.detail}")
        raise typer.Exit(1)

    task_group_id = tasks[0].get("task_group_id") if tasks else None
    task_ids = [t["task_id"] for t in tasks]

    file_name_map: dict[str, str] = {}
    for i, t in enumerate(tasks):
        tid = t["task_id"]
        fn = t.get("file_name")
        if fn:
            file_name_map[tid] = fn
        elif i < len(upload_map):
            fp, _ = upload_map[i]
            file_name_map[tid] = fp.name

    if not c.quiet:
        out.success(f"已创建 {len(tasks)} 个任务 (批次: {task_group_id or 'N/A'})")

    if no_wait:
        summary = {
            "task_group_id": task_group_id,
            "task_count": len(tasks),
            "task_ids": task_ids,
            "files": [fp.name for fp, _ in upload_map],
            "upload_failures": upload_failures,
        }
        if json_summary:
            out.print_json(summary)
        else:
            out.render(c.output_format, data=tasks,
                       title="任务已提交（批量）",
                       columns=["文件", "task_id", "状态"],
                       rows=[[file_name_map.get(t["task_id"], ""), t["task_id"][:12] + "...", t["status"]] for t in tasks],
                       footer=f"批次 ID: {task_group_id or 'N/A'}")
        if upload_failures:
            raise typer.Exit(1)
        return

    # Phase 3: Poll batch progress
    if not c.quiet:
        out.info(f"[3/4] 等待 {len(task_ids)} 个任务完成...")

    completed: dict[str, dict] = {}
    task_id_set = set(task_ids)
    start_time = time.time()

    while len(completed) < len(task_ids):
        if time.time() - start_time > wait_timeout:
            out.error(f"批量等待超时 ({wait_timeout}s)")
            break

        try:
            if task_group_id:
                group_data = c.client.list_group_tasks(task_group_id, page_size=500)
                batch_tasks = group_data.get("items", [])
            else:
                batch_tasks = [c.client.get_task(tid) for tid in task_ids if tid not in completed]

            for t in batch_tasks:
                tid = t["task_id"]
                if tid in completed or tid not in task_id_set:
                    continue
                is_terminal = t.get("is_terminal", t["status"] in ("SUCCEEDED", "CANCELED"))
                if is_terminal:
                    completed[tid] = t
                    if not c.quiet:
                        elapsed = int(time.time() - start_time)
                        status_icon = "✓" if t["status"] == "SUCCEEDED" else "✗"
                        out.info(f"  [{elapsed}s] {status_icon} {file_name_map.get(tid, tid[:12])} → {t['status']} "
                                 f"({len(completed)}/{len(task_ids)})")
        except APIError as poll_err:
            if not c.quiet:
                out.error(f"  轮询出错: {poll_err.detail} (将继续重试)")

        if len(completed) < len(task_ids):
            time.sleep(poll_interval)

    succeeded = [t for t in completed.values() if t["status"] == "SUCCEEDED"]
    failed = [t for t in completed.values() if t["status"] != "SUCCEEDED"]
    not_finished = len(task_ids) - len(completed)

    server_usage: dict[str, int] = {}
    for t in completed.values():
        sid = t.get("assigned_server_id")
        if sid:
            server_usage[sid] = server_usage.get(sid, 0) + 1

    if not c.quiet:
        elapsed = int(time.time() - start_time)
        out.info(f"  批量完成: {len(succeeded)} 成功, {len(failed)} 失败, {not_finished} 未完成 (耗时 {elapsed}s)")

    # Phase 4: Download results
    results = []
    if download_results and succeeded:
        if not c.quiet:
            out.info(f"[4/4] 下载 {len(succeeded)} 个结果...")
        suffix = {"json": ".json", "txt": ".txt", "srt": ".srt"}.get(fmt, ".json")
        output_dir.mkdir(parents=True, exist_ok=True)

        seen_names: dict[str, int] = {}
        for task in succeeded:
            tid = task["task_id"]
            try:
                content = c.client.get_result(tid, fmt=fmt)
                base_name = file_name_map.get(tid, tid[:12])
                if base_name in seen_names:
                    seen_names[base_name] += 1
                    dedup_name = f"{base_name}_{seen_names[base_name]}"
                else:
                    seen_names[base_name] = 0
                    dedup_name = base_name
                dest = output_dir / f"{dedup_name}_result{suffix}"
                dest.write_text(content, encoding="utf-8")
                results.append({"file": file_name_map.get(tid, ""), "task_id": tid,
                                "status": "SUCCEEDED", "output": str(dest)})
            except APIError as e:
                results.append({"file": file_name_map.get(tid, ""), "task_id": tid,
                                "status": "DOWNLOAD_FAILED", "output": e.detail})

    for task in failed:
        tid = task["task_id"]
        results.append({"file": file_name_map.get(tid, ""), "task_id": tid,
                        "status": task["status"],
                        "output": task.get("error_message", "-") or "-"})

    for tid in task_ids:
        if tid not in completed:
            results.append({"file": file_name_map.get(tid, ""), "task_id": tid,
                            "status": "TIMEOUT", "output": "-"})

    # Output summary
    if json_summary:
        summary = {
            "task_group_id": task_group_id,
            "total_input_files": total,
            "upload_failures": upload_failures,
            "tasks_created": len(task_ids),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "timeout": not_finished,
            "elapsed_seconds": int(time.time() - start_time),
            "server_usage": server_usage,
            "results": results,
        }
        out.print_json(summary)
    else:
        usage_str = ", ".join(f"{k}: {v}" for k, v in sorted(server_usage.items()))
        out.render(
            c.output_format, data=results,
            title=f"批量转写结果 (批次: {task_group_id or 'N/A'})",
            columns=["文件", "task_id", "状态", "输出"],
            rows=[[r.get("file", ""), r.get("task_id", "")[:12] + "...",
                   r.get("status", ""), r.get("output", "-")] for r in results],
            footer=f"成功 {len(succeeded)} / 失败 {len(failed)} / 超时 {not_finished}"
                   + (f"\n  调度分布: {usage_str}" if server_usage else ""),
        )

    if failed or not_finished or upload_failures:
        raise typer.Exit(1)
