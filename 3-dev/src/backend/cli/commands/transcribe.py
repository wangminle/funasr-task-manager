"""Transcribe command - one-shot: upload → create → wait → download."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from cli.api_client import APIError
from cli import output as out


def transcribe(
    ctx: typer.Context,
    files: list[Path] = typer.Argument(..., help="要转写的文件路径（支持多个）"),
    language: str = typer.Option("auto", "--language", "-l", help="识别语言"),
    hotwords: Optional[str] = typer.Option(None, "--hotwords", help="热词，逗号分隔"),
    fmt: str = typer.Option("json", "--format", "-f", help="结果格式: json/txt/srt"),
    output_dir: Path = typer.Option(".", "--output-dir", "-d", help="结果输出目录"),
    save: Optional[Path] = typer.Option(None, "--save", help="保存到指定文件（单文件时）"),
    callback: Optional[str] = typer.Option(None, "--callback", help="回调地址"),
    no_wait: bool = typer.Option(False, "--no-wait", help="不等待完成"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="轮询间隔(秒)"),
    wait_timeout: float = typer.Option(3600.0, "--timeout", help="单任务超时(秒)"),
):
    """一键转写：上传 → 创建任务 → 等待完成 → 下载结果。"""
    from cli.main import get_ctx
    from cli.progress import wait_for_task
    c = get_ctx(ctx)

    existing = [f for f in files if f.exists()]
    if not existing:
        out.error("没有找到有效的文件")
        raise typer.Exit(1)

    options = {}
    if hotwords:
        options["hotwords"] = hotwords

    results = []
    error_count = 0

    for fp in existing:
        if not c.quiet:
            out.info(f"[1/4] 上传: {fp.name}")
        try:
            file_data = c.client.upload_file(fp)
        except APIError as e:
            out.error(f"上传失败 {fp.name}: {e.detail}")
            results.append({"file": fp.name, "task_id": "-", "status": "UPLOAD_FAILED"})
            error_count += 1
            continue

        if not c.quiet:
            out.info(f"[2/4] 创建任务: {file_data['file_id']}")
        items = [{"file_id": file_data["file_id"], "language": language, "options": options or None}]
        cb = {"url": callback} if callback else None
        try:
            tasks = c.client.create_tasks(items, callback=cb)
        except APIError as e:
            out.error(f"创建任务失败: {e.detail}")
            results.append({"file": fp.name, "task_id": "-", "status": "CREATE_FAILED"})
            error_count += 1
            continue

        task = tasks[0]
        task_id = task["task_id"]

        if no_wait:
            results.append({"file": fp.name, "task_id": task_id, "status": task["status"]})
            continue

        if not c.quiet:
            out.info(f"[3/4] 等待完成: {task_id}")
        try:
            task = wait_for_task(c.client, task_id, poll_interval=poll_interval,
                                 timeout=wait_timeout, quiet=c.quiet)
        except TimeoutError:
            out.error(f"任务超时: {task_id}")
            results.append({"file": fp.name, "task_id": task_id, "status": "TIMEOUT"})
            error_count += 1
            continue

        if task["status"] != "SUCCEEDED":
            out.error(f"任务失败: {task_id} [{task['status']}] {task.get('error_message', '')}")
            results.append({"file": fp.name, "task_id": task_id, "status": task["status"]})
            error_count += 1
            continue

        if not c.quiet:
            out.info(f"[4/4] 下载结果: {task_id}")
        try:
            content = c.client.get_result(task_id, fmt=fmt)
        except APIError as e:
            out.error(f"下载结果失败: {e.detail}")
            results.append({"file": fp.name, "task_id": task_id, "status": "DOWNLOAD_FAILED"})
            error_count += 1
            continue

        if save and len(existing) == 1:
            dest = save
        else:
            suffix = {"json": ".json", "txt": ".txt", "srt": ".srt"}.get(fmt, ".json")
            dest = output_dir / f"{fp.stem}_result{suffix}"

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

        if not c.quiet:
            out.success(f"完成: {fp.name} → {dest}")
        results.append({"file": fp.name, "task_id": task_id, "status": "SUCCEEDED", "output": str(dest)})

    if c.quiet and results:
        out.render("json", data=results)
    elif results:
        out.render(
            c.output_format, data=results,
            title="转写结果", columns=["文件", "task_id", "状态", "输出"],
            rows=[[r.get("file", ""), r.get("task_id", "")[:12] + "...",
                   r.get("status", ""), r.get("output", "-")] for r in results],
        )

    if error_count > 0:
        raise typer.Exit(1)
