"""Config management commands: set, get, list."""

from __future__ import annotations

from typing import Optional

import typer

from cli import config_store
from cli import output as out

app = typer.Typer()


@app.command(name="set")
def config_set(
    key: str = typer.Argument(..., help="配置键名 (server/api_key/output)"),
    value: str = typer.Argument(..., help="配置值"),
):
    """设置 CLI 配置项。"""
    config_store.set_value(key, value)
    out.success(f"配置已保存: {key} = {value}")


@app.command(name="get")
def config_get(
    key: str = typer.Argument(..., help="配置键名"),
):
    """查看 CLI 配置项。"""
    val = config_store.get(key)
    if val is None:
        out.error(f"配置项不存在: {key}")
        raise typer.Exit(1)
    print(val)


@app.command(name="list")
def config_list():
    """查看所有 CLI 配置。"""
    data = config_store.get_all()
    out.render(
        "table", data=data, title="CLI 配置",
        columns=["键", "值"],
        rows=[[k, str(v)] for k, v in data.items()],
    )
