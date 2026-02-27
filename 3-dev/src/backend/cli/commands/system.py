"""System commands: health, stats, metrics."""

from __future__ import annotations

import sys

import typer

from cli.api_client import APIError
from cli import output as out


def health(ctx: typer.Context):
    """检查系统健康状态（exit code 0=健康, 1=异常）。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        data = c.client.health()
    except (APIError, Exception) as e:
        out.error(f"健康检查失败: {e}")
        raise typer.Exit(1)

    out.render(c.output_format, data=data, title="健康检查",
               columns=["字段", "值"],
               rows=[[k, str(v)] for k, v in data.items()])


def stats(ctx: typer.Context):
    """查看系统统计概览。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        data = c.client.stats()
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.render(
        c.output_format, data=data, title="系统统计",
        columns=["指标", "值"],
        rows=[
            ["ASR 节点 (在线/总计)", f"{data.get('server_online', 0)}/{data.get('server_total', 0)}"],
            ["槽位 (已用/总计)", f"{data.get('slots_used', 0)}/{data.get('slots_total', 0)}"],
            ["队列深度", str(data.get("queue_depth", 0))],
            ["今日完成", str(data.get("tasks_today_completed", 0))],
            ["今日失败", str(data.get("tasks_today_failed", 0))],
            ["成功率 (24h)", f"{data.get('success_rate_24h', 0)}%"],
            ["平均 RTF", str(data.get("avg_rtf", "-") or "-")],
        ],
    )


def metrics(ctx: typer.Context):
    """查看 Prometheus 原始指标。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    try:
        text = c.client.metrics()
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    print(text)
