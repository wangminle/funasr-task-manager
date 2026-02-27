"""Upload command - upload one or more audio/video files."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from cli.api_client import APIError
from cli import output as out


def upload(
    ctx: typer.Context,
    files: list[Path] = typer.Argument(..., help="要上传的文件路径（支持多个）"),
    language: str = typer.Option("auto", "--language", "-l", help="识别语言"),
    hotwords: Optional[str] = typer.Option(None, "--hotwords", help="热词，逗号分隔"),
    callback: Optional[str] = typer.Option(None, "--callback", help="回调地址"),
    callback_secret: Optional[str] = typer.Option(None, "--callback-secret", help="回调签名密钥"),
    create_task: bool = typer.Option(False, "--create-task", help="上传后自动创建任务"),
):
    """上传一个或多个文件到 ASR 服务。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    uploaded = []
    for fp in files:
        if not fp.exists():
            out.error(f"文件不存在: {fp}")
            continue
        try:
            result = c.client.upload_file(fp)
            uploaded.append(result)
            if not c.quiet:
                out.success(f"上传成功: {fp.name} → {result['file_id']}")
        except APIError as e:
            out.error(f"上传失败 {fp.name}: {e.detail}")

    if not uploaded:
        raise typer.Exit(1)

    task_create_failed = False
    if create_task and uploaded:
        options = {}
        if hotwords:
            options["hotwords"] = hotwords
        items = [
            {"file_id": u["file_id"], "language": language, "options": options or None}
            for u in uploaded
        ]
        cb = {"url": callback, "secret": callback_secret} if callback else None
        try:
            tasks = c.client.create_tasks(items, callback=cb)
            if not c.quiet:
                out.success(f"已创建 {len(tasks)} 个任务")
            uploaded = [{"file": u, "task": t} for u, t in zip(uploaded, tasks)]
        except APIError as e:
            out.error(f"创建任务失败: {e.detail}")
            task_create_failed = True

    def _row(u: dict) -> list[str]:
        if "file" in u and "task" in u:
            f, t = u["file"], u["task"]
            return [f.get("file_id", ""), f.get("original_name", ""),
                    str(f.get("size_bytes", "")), t.get("status", "")]
        return [u.get("file_id", ""), u.get("original_name", ""),
                str(u.get("size_bytes", "")), u.get("status", "")]

    columns = ["file_id", "original_name", "size_bytes", "status"]
    if any("task" in u for u in uploaded):
        columns = ["file_id", "original_name", "task_id", "task_status"]

        def _row(u: dict) -> list[str]:  # noqa: F811
            f, t = u.get("file", u), u.get("task", {})
            return [f.get("file_id", ""), f.get("original_name", ""),
                    t.get("task_id", ""), t.get("status", "")]

    out.render(c.output_format, data=uploaded if len(uploaded) > 1 else uploaded[0],
               title="上传结果", columns=columns,
               rows=[_row(u) for u in uploaded])

    if task_create_failed:
        raise typer.Exit(1)
