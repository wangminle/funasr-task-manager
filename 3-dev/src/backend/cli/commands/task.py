"""Task management commands: create, list, info, cancel, result, wait, progress."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from cli.api_client import APIError
from cli import output as out

app = typer.Typer()


@app.command(name="create")
def create(
    ctx: typer.Context,
    file_ids: list[str] = typer.Argument(..., help="文件 ID（支持多个）"),
    language: str = typer.Option("zh", "--language", "-l", help="识别语言"),
    hotwords: Optional[str] = typer.Option(None, "--hotwords", help="热词，逗号分隔"),
    callback: Optional[str] = typer.Option(None, "--callback", help="回调地址"),
    callback_secret: Optional[str] = typer.Option(None, "--callback-secret", help="回调签名密钥"),
    wait: bool = typer.Option(False, "--wait", "-w", help="等待所有任务完成"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="轮询间隔(秒)"),
    wait_timeout: float = typer.Option(3600.0, "--timeout", help="等待超时(秒)"),
):
    """创建转写任务（支持批量）。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    options = {}
    if hotwords:
        options["hotwords"] = hotwords
    items = [{"file_id": fid, "language": language, "options": options or None} for fid in file_ids]
    cb = {"url": callback, "secret": callback_secret} if callback else None

    try:
        tasks = c.client.create_tasks(items, callback=cb)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    if not c.quiet:
        out.success(f"已创建 {len(tasks)} 个任务")

    if wait:
        from cli.progress import wait_for_task
        for t in tasks:
            wait_for_task(c.client, t["task_id"], poll_interval=poll_interval,
                          timeout=wait_timeout, quiet=c.quiet)

    out.render(
        c.output_format, data=tasks,
        title="创建的任务", columns=["task_id", "file_id", "status", "language"],
        rows=[[t["task_id"], t["file_id"], t["status"], t.get("language", "")] for t in tasks],
    )


@app.command(name="list")
def list_tasks(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", help="按状态筛选"),
    search: Optional[str] = typer.Option(None, "--search", help="搜索文件名/任务ID"),
    page: int = typer.Option(1, "--page", help="页码"),
    page_size: int = typer.Option(20, "--page-size", help="每页数量"),
):
    """查询任务列表。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        data = c.client.list_tasks(status=status, search=search, page=page, page_size=page_size)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    items = data.get("items", [])
    total = data.get("total", 0)

    out.render(
        c.output_format, data=data,
        title="任务列表",
        columns=["task_id", "状态", "进度", "语言", "创建时间"],
        rows=[[t["task_id"][:12] + "...", t["status"], f"{t['progress']*100:.0f}%",
               t.get("language", ""), t.get("created_at", "")[:19]] for t in items],
        footer=f"共 {total} 条 · 第 {page} 页 · 每页 {page_size} 条",
    )


@app.command(name="info")
def task_info(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="任务 ID"),
):
    """查看任务详情。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        task = c.client.get_task(task_id)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.render(
        c.output_format, data=task, title=f"任务详情: {task_id}",
        columns=["字段", "值"],
        rows=[
            ["任务 ID", task.get("task_id", "")],
            ["状态", task.get("status", "")],
            ["进度", f"{task.get('progress', 0)*100:.1f}%"],
            ["ETA", f"{task.get('eta_seconds', '-')}s"],
            ["语言", task.get("language", "")],
            ["文件 ID", task.get("file_id", "")],
            ["分配服务器", task.get("assigned_server_id", "-")],
            ["重试次数", str(task.get("retry_count", 0))],
            ["错误信息", task.get("error_message", "-") or "-"],
            ["创建时间", task.get("created_at", "")],
            ["完成时间", task.get("completed_at", "-") or "-"],
        ],
    )


@app.command(name="cancel")
def cancel(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="任务 ID"),
):
    """取消任务。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        task = c.client.cancel_task(task_id)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.success(f"任务已取消: {task_id}")
    out.render(c.output_format, data=task)


@app.command(name="result")
def result(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="任务 ID"),
    fmt: str = typer.Option("json", "--format", "-f", help="结果格式: json/txt/srt"),
    save: Optional[Path] = typer.Option(None, "--save", help="保存到文件"),
):
    """下载转写结果。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        content = c.client.get_result(task_id, fmt=fmt)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    if save:
        save.write_text(content, encoding="utf-8")
        if not c.quiet:
            out.success(f"结果已保存: {save}")
    else:
        print(content)


@app.command(name="wait")
def wait(
    ctx: typer.Context,
    task_ids: list[str] = typer.Argument(..., help="任务 ID（支持多个）"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="轮询间隔(秒)"),
    wait_timeout: float = typer.Option(3600.0, "--timeout", help="最大等待时间(秒)"),
):
    """等待任务完成。"""
    from cli.main import get_ctx
    from cli.progress import wait_for_task
    c = get_ctx(ctx)

    results = []
    for tid in task_ids:
        try:
            task = wait_for_task(c.client, tid, poll_interval=poll_interval,
                                 timeout=wait_timeout, quiet=c.quiet)
            results.append(task)
        except TimeoutError as e:
            out.error(str(e))
            raise typer.Exit(1)
        except APIError as e:
            out.error(f"任务 {tid}: {e.detail}")
            raise typer.Exit(1)

    out.render(
        c.output_format, data=results,
        title="任务完成", columns=["task_id", "status", "progress"],
        rows=[[t["task_id"], t["status"], f"{t['progress']*100:.0f}%"] for t in results],
    )


@app.command(name="progress")
def progress(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="任务 ID"),
):
    """实时流式查看任务进度（SSE）。"""
    from cli.main import get_ctx
    from cli.progress import stream_progress
    c = get_ctx(ctx)

    stream_progress(c.client, task_id)
