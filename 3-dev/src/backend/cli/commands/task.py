"""Task management commands: create, list, info, cancel, result, wait, progress."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import typer

from cli import output as out
from cli.api_client import APIError
from cli.path_utils import get_default_download_dir

app = typer.Typer()


def _result_output_filename(
    original_name: str | None,
    task_id: str,
    fmt: str,
    seen_names: dict[str, int] | None = None,
) -> str:
    """Build the user-visible exported result filename from the source file name."""
    suffix_map = {"json": ".json", "txt": ".txt", "srt": ".srt"}
    suffix = suffix_map.get(fmt, f".{fmt}")

    source_name = Path(str(original_name or "")).name
    if source_name.startswith("."):
        source_name = source_name[1:]
    if source_name:
        source_path = Path(source_name)
        stem = source_path.stem if source_path.suffix else source_path.name
    else:
        stem = task_id[:12]
    stem = stem or task_id[:12]

    if seen_names is None:
        return f"{stem}_result{suffix}"

    base_key = f"{stem}_result{suffix}"
    count = seen_names.get(base_key, 0) + 1
    seen_names[base_key] = count
    if count == 1:
        return base_key
    return f"{stem}_{count}_result{suffix}"


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
    segment_level: str = typer.Option("10m", "--segment-level", help="切分策略: off/10m/20m/30m"),
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
        tasks = c.client.create_tasks(items, callback=cb, segment_level=segment_level)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    task_group_id = tasks[0].get("task_group_id") if tasks else None
    if not c.quiet:
        out.success(f"已创建 {len(tasks)} 个任务" + (f" (批次: {task_group_id})" if task_group_id else ""))

    if wait:
        from cli.progress import wait_for_task
        for t in tasks:
            wait_for_task(c.client, t["task_id"], poll_interval=poll_interval,
                          timeout=wait_timeout, quiet=c.quiet)

    out.render(
        c.output_format, data=tasks,
        title="创建的任务", columns=["task_id", "file_id", "status", "language", "task_group_id"],
        rows=[[t["task_id"], t["file_id"], t["status"], t.get("language", ""),
               t.get("task_group_id", "-") or "-"] for t in tasks],
    )


@app.command(name="list")
def list_tasks(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(None, "--status", help="按状态筛选"),
    search: Optional[str] = typer.Option(None, "--search", help="搜索文件名/任务ID"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="按批次 ID 筛选"),
    page: int = typer.Option(1, "--page", help="页码"),
    page_size: int = typer.Option(20, "--page-size", help="每页数量"),
):
    """查询任务列表。支持 --group 按批次筛选。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        if group:
            data = c.client.list_group_tasks(group, page=page, page_size=page_size)
            items = data.get("items", [])
            total = data.get("total", 0)
            title = f"批次 {group} 的任务"
        else:
            data = c.client.list_tasks(status=status, search=search, page=page, page_size=page_size)
            items = data.get("items", [])
            total = data.get("total", 0)
            title = "任务列表"
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.render(
        c.output_format, data=data,
        title=title,
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
            ["批次 ID", task.get("task_group_id", "-") or "-"],
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
    task_id: Optional[str] = typer.Argument(None, help="任务 ID"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="批次 ID（下载整批结果）"),
    fmt: str = typer.Option("json", "--format", "-f", help="结果格式: json/txt/srt 或逗号分隔多格式如 txt,json,srt"),
    save: Optional[Path] = typer.Option(None, "--save", help="保存到文件"),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        "-d",
        help="结果下载目录；单任务会按源文件名自动命名，批量默认写入仓库根目录 runtime/storage/downloads",
    ),
):
    """下载转写结果。支持 --group 下载整批结果，支持多格式同时导出。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if group:
        formats = [f.strip() for f in fmt.split(",") if f.strip()]
        _download_group_results(c, group, formats, output_dir or get_default_download_dir())
        return

    if not task_id:
        out.error("请指定任务 ID 或使用 --group 下载批次结果")
        raise typer.Exit(1)

    formats = [f.strip() for f in fmt.split(",") if f.strip()]

    if save:
        if len(formats) > 1 and not c.quiet:
            out.info(f"--save 仅支持单格式，将使用 {formats[0]}（忽略 {', '.join(formats[1:])}）")
        try:
            content = c.client.get_result(task_id, fmt=formats[0])
        except APIError as e:
            out.error(e.detail)
            raise typer.Exit(1)
        save.write_text(content, encoding="utf-8")
        if not c.quiet:
            out.success(f"结果已保存: {save}")
    elif output_dir:
        try:
            task = c.client.get_task(task_id)
        except APIError as e:
            out.error(e.detail)
            raise typer.Exit(1) from None

        output_dir.mkdir(parents=True, exist_ok=True)
        saved_count = 0
        for f in formats:
            try:
                content = c.client.get_result(task_id, fmt=f)
            except APIError as e:
                out.error(f"下载失败 ({f}): {e.detail}")
                continue
            result_name = _result_output_filename(task.get("file_name"), task_id, f)
            dest = output_dir / result_name
            dest.write_text(content, encoding="utf-8")
            saved_count += 1
            if not c.quiet:
                out.success(f"结果已保存: {dest}")
        if saved_count == 0:
            out.error("所有格式的结果下载均失败")
            raise typer.Exit(1)
    else:
        try:
            content = c.client.get_result(task_id, fmt=formats[0])
        except APIError as e:
            out.error(e.detail)
            raise typer.Exit(1)
        print(content)


def _download_group_results(c, group_id, formats, output_dir):
    """Download all results for a task group with multi-format support and summary."""
    import json as _json

    if not formats:
        formats = ["json"]

    try:
        group_stats = c.client.get_task_group(group_id)
        group_data = c.client.list_group_tasks(group_id, page_size=500)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    tasks = group_data.get("items", [])
    succeeded = [t for t in tasks if t["status"] == "SUCCEEDED"]
    failed = [t for t in tasks if t["status"] == "FAILED"]

    if not succeeded:
        out.error(f"批次 {group_id} 中没有已完成的任务")
        raise typer.Exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    summary_items = []

    seen_names: dict[str, int] = {}
    for t in tasks:
        item = {
            "task_id": t["task_id"],
            "file_id": t.get("file_id", ""),
            "status": t["status"],
            "outputs": {},
        }

        if t["status"] == "SUCCEEDED":
            tid = t["task_id"]
            for fmt in formats:
                try:
                    content = c.client.get_result(tid, fmt=fmt)
                    result_name = _result_output_filename(t.get("file_name"), tid, fmt, seen_names)
                    dest = output_dir / result_name
                    dest.write_text(content, encoding="utf-8")
                    item["outputs"][fmt] = str(dest)
                    downloaded += 1
                except APIError as e:
                    item["outputs"][fmt] = f"FAILED: {e.detail}"
                    out.error(f"下载失败 {tid} ({fmt}): {e.detail}")
        elif t["status"] == "FAILED":
            item["error"] = t.get("error_message", "")

        summary_items.append(item)

    summary = {
        "task_group_id": group_id,
        "total_tasks": group_stats.get("total", len(tasks)),
        "succeeded": group_stats.get("succeeded", len(succeeded)),
        "failed": group_stats.get("failed", len(failed)),
        "formats_exported": formats,
        "output_directory": str(output_dir),
        "items": summary_items,
    }
    summary_path = output_dir / "batch-summary.json"
    summary_path.write_text(_json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if not c.quiet:
        file_count = downloaded
        out.success(
            f"已下载 {file_count} 个结果文件到 {output_dir}\n"
            f"  格式: {', '.join(formats)}\n"
            f"  摘要: {summary_path}"
        )


@app.command(name="wait")
def wait(
    ctx: typer.Context,
    task_ids: Optional[list[str]] = typer.Argument(None, help="任务 ID（支持多个）"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="等待整批完成"),
    poll_interval: float = typer.Option(5.0, "--poll-interval", help="轮询间隔(秒)"),
    wait_timeout: float = typer.Option(3600.0, "--timeout", help="最大等待时间(秒)"),
):
    """等待任务完成。支持 --group 等待整批完成。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if group:
        _wait_group(c, group, poll_interval, wait_timeout)
        return

    if not task_ids:
        out.error("请指定任务 ID 或使用 --group 等待批次")
        raise typer.Exit(1)

    from cli.progress import wait_for_task
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


def _wait_group(c, group_id, poll_interval, wait_timeout):
    """Wait for all tasks in a group to reach terminal state."""
    start = time.time()
    terminal = {"SUCCEEDED", "FAILED", "CANCELED"}

    while True:
        try:
            stats = c.client.get_task_group(group_id)
        except APIError as e:
            out.error(e.detail)
            raise typer.Exit(1)

        total = stats.get("total", 0)
        done = stats.get("succeeded", 0) + stats.get("failed", 0) + stats.get("canceled", 0)
        progress = stats.get("progress", 0)
        elapsed = int(time.time() - start)

        if not c.quiet:
            out.info(f"  [{elapsed}s] 完成: {done}/{total} | 进度: {progress*100:.0f}%")

        if stats.get("is_complete"):
            if not c.quiet:
                out.success(
                    f"批次 {group_id} 已完成: "
                    f"{stats.get('succeeded', 0)} 成功, "
                    f"{stats.get('failed', 0)} 失败, "
                    f"{stats.get('canceled', 0)} 取消"
                )
            out.render(c.output_format, data=stats)
            if stats.get("failed", 0) > 0:
                raise typer.Exit(1)
            return

        if time.time() - start > wait_timeout:
            out.error(f"批次等待超时 ({wait_timeout}s)")
            raise typer.Exit(1)

        time.sleep(poll_interval)


@app.command(name="delete")
def delete(
    ctx: typer.Context,
    task_id: Optional[str] = typer.Argument(None, help="任务 ID"),
    group: Optional[str] = typer.Option(None, "--group", "-g", help="删除整批任务"),
):
    """删除任务。支持 --group 删除整批。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if group:
        try:
            result = c.client.delete_task_group(group)
            out.success(f"已删除批次 {group}: {result.get('deleted', 0)} 个任务 "
                        f"(跳过 {result.get('skipped_active', 0)} 个运行中)")
            out.render(c.output_format, data=result)
        except APIError as e:
            out.error(e.detail)
            raise typer.Exit(1)
        return

    if not task_id:
        out.error("请指定任务 ID 或使用 --group 删除批次")
        raise typer.Exit(1)

    out.error("单任务删除请使用 task cancel <task_id>")
    raise typer.Exit(1)


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
