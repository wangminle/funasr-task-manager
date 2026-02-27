"""File info command."""

from __future__ import annotations

import typer

from cli.api_client import APIError
from cli import output as out

app = typer.Typer()


@app.command(name="info")
def file_info(
    ctx: typer.Context,
    file_id: str = typer.Argument(..., help="文件 ID"),
):
    """查看文件元信息。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        info = c.client.file_info(file_id)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.render(
        c.output_format, data=info, title=f"文件信息: {file_id}",
        columns=["字段", "值"],
        rows=[
            ["文件 ID", info.get("file_id", "")],
            ["文件名", info.get("original_name", "")],
            ["媒体类型", info.get("media_type", "")],
            ["MIME", info.get("mime", "")],
            ["时长(秒)", str(info.get("duration_sec", "-"))],
            ["编码", info.get("codec", "-")],
            ["采样率", str(info.get("sample_rate", "-"))],
            ["声道", str(info.get("channels", "-"))],
            ["文件大小", f"{info.get('size_bytes', 0):,} bytes"],
            ["状态", info.get("status", "")],
        ],
    )
