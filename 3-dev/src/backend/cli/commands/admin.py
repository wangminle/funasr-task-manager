"""Administrative recovery commands."""

from __future__ import annotations

from typing import Optional

import typer

from cli import output as out
from cli.api_client import APIError

app = typer.Typer()


@app.command(name="active-slots")
def active_slots(ctx: typer.Context):
    """显示每台服务器当前 slot 占用来源。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        data = c.client.active_slots()
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    rows = []
    for server in data.get("servers", []):
        rows.append([
            server.get("server_id", ""),
            server.get("status", ""),
            str(server.get("enabled", "")),
            str(server.get("active_slots", 0)),
            str(server.get("max_concurrency", "")),
            str(len(server.get("whole_tasks", []))),
            str(len(server.get("segments", []))),
            str(sum(1 for seg in server.get("segments", []) if seg.get("is_zombie"))),
        ])

    out.render(
        c.output_format,
        data=data,
        title="Active Slots",
        columns=["服务器", "状态", "启用", "占用", "并发", "整任务", "分段", "僵尸段"],
        rows=rows,
        footer=(
            f"总占用 {data.get('total_active_slots', 0)} slots · "
            f"僵尸段 {data.get('zombie_segments', 0)}"
        ),
    )


@app.command(name="emergency-stop")
def emergency_stop(
    ctx: typer.Context,
    scope: str = typer.Option("all", "--scope", help="急停范围: all/group"),
    group_id: Optional[str] = typer.Option(None, "--group-id", help="scope=group 时的任务组 ID"),
    confirm: bool = typer.Option(False, "--confirm", help="真正执行急停；不带时仅 dry-run"),
):
    """急停活跃任务并释放 slot。默认只预演，不修改状态。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if scope not in {"all", "group"}:
        out.error("--scope 仅支持 all 或 group")
        raise typer.Exit(1)
    if scope == "group" and not group_id:
        out.error("--scope group 必须提供 --group-id")
        raise typer.Exit(1)

    try:
        data = c.client.emergency_stop(
            scope=scope,
            group_id=group_id,
            dry_run=not confirm,
            confirm=confirm,
        )
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    rows = [
        ["范围", data.get("scope", "")],
        ["任务组", data.get("group_id") or "-"],
        ["dry_run", str(data.get("dry_run", ""))],
        ["待取消任务", str(data.get("tasks_to_cancel", 0))],
        ["待释放 segment", str(data.get("segments_to_release", 0))],
        ["急停前 slot", str(data.get("active_slots_before", 0))],
        ["急停前僵尸段", str(data.get("zombie_segments_before", 0))],
        ["已取消任务", str(data.get("tasks_canceled", 0))],
        ["已释放 segment", str(data.get("segments_released", 0))],
    ]
    if "active_slots_after" in data:
        rows.append(["急停后 slot", str(data.get("active_slots_after", 0))])
        rows.append(["急停后僵尸段", str(data.get("zombie_segments_after", 0))])

    footer = "未执行修改；确认无误后追加 --confirm" if data.get("dry_run") else "急停已执行"
    out.render(
        c.output_format,
        data=data,
        title="Emergency Stop",
        columns=["字段", "值"],
        rows=rows,
        footer=footer,
    )
