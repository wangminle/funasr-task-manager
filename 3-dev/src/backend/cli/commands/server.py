"""Server management commands: list, register, delete."""

from __future__ import annotations

from typing import Optional

import typer

from cli.api_client import APIError
from cli import output as out

app = typer.Typer()


@app.command(name="list")
def server_list(ctx: typer.Context):
    """查看所有 ASR 节点状态。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        servers = c.client.list_servers()
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.render(
        c.output_format, data=servers,
        title="ASR 节点列表",
        columns=["server_id", "名称", "地址", "协议", "状态", "槽位", "RTF"],
        rows=[
            [s["server_id"], s.get("name", ""), f"{s['host']}:{s['port']}",
             s.get("protocol_version", ""), s.get("status", ""),
             str(s.get("max_concurrency", "")),
             str(s.get("rtf_baseline", "-") or "-")]
            for s in servers
        ],
    )


@app.command(name="register")
def register(
    ctx: typer.Context,
    server_id: str = typer.Option(..., "--id", help="节点 ID"),
    name: Optional[str] = typer.Option(None, "--name", help="节点名称"),
    host: str = typer.Option(..., "--host", help="主机地址"),
    port: int = typer.Option(..., "--port", help="端口"),
    protocol: str = typer.Option("v2_new", "--protocol", help="协议版本: v1_old/v2_new"),
    max_concurrency: int = typer.Option(4, "--max-concurrency", help="最大并发数"),
):
    """注册新的 ASR 节点。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    data = {
        "server_id": server_id,
        "name": name or server_id,
        "host": host,
        "port": port,
        "protocol_version": protocol,
        "max_concurrency": max_concurrency,
    }
    try:
        result = c.client.register_server(data)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.success(f"节点注册成功: {server_id}")
    out.render(c.output_format, data=result)


@app.command(name="delete")
def delete(
    ctx: typer.Context,
    server_id: str = typer.Argument(..., help="节点 ID"),
):
    """删除 ASR 节点。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        c.client.delete_server(server_id)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.success(f"节点已删除: {server_id}")
