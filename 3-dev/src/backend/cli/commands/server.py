"""Server management commands: list, register, delete, probe, benchmark, update."""

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
        columns=["server_id", "名称", "地址", "状态", "槽位", "单线程RTF", "吞吐量RTF", "测试并发"],
        rows=[
            [s["server_id"], s.get("name", ""), f"{s['host']}:{s['port']}",
             s.get("status", ""),
             str(s.get("max_concurrency", "")),
             str(s.get("rtf_baseline", "-") or "-"),
             str(s.get("throughput_rtf", "-") or "-"),
             str(s.get("benchmark_concurrency", "-") or "-")]
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


@app.command(name="probe")
def probe(
    ctx: typer.Context,
    server_id: str = typer.Argument(..., help="节点 ID"),
    level: str = typer.Option(
        "offline_light", "--level", "-l",
        help="探测级别: connect_only(仅 WebSocket)/offline_light/twopass_full",
    ),
):
    """探测 ASR 节点能力和连通性。

    probe 仅用于连通性和协议能力检查，不更新 benchmark 结果或 RTF 基线。
    """
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if not c.quiet:
        out.info(f"正在探测 {server_id} (级别: {level})...")

    try:
        result = c.client.probe_server(server_id, level=level)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    reachable = result.get("reachable", False)
    if reachable:
        out.success(f"节点 {server_id} 可达")
    else:
        out.error(f"节点 {server_id} 不可达: {result.get('error', 'unknown')}")

    out.render(
        c.output_format, data=result, title=f"探测结果: {server_id}",
        columns=["字段", "值"],
        rows=[
            ["可达", "✅" if reachable else "❌"],
            ["响应", "✅" if result.get("responsive") else "❌"],
            ["服务类型", result.get("inferred_server_type", "-")],
            ["支持 offline", "✅" if result.get("supports_offline") else "-"],
            ["支持 2pass", "✅" if result.get("supports_2pass") else "-"],
            ["支持 online", "✅" if result.get("supports_online") else "-"],
            ["探测耗时", f"{result.get('probe_duration_ms', 0):.0f}ms"],
        ],
    )

    if not reachable:
        raise typer.Exit(1)


@app.command(name="benchmark")
def benchmark(
    ctx: typer.Context,
    server_id: Optional[str] = typer.Argument(None, help="可选：仅对单个节点执行 benchmark"),
):
    """使用公开 benchmark 样本对单个或全部在线节点执行真实 RTF 基准测试。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    if not c.quiet:
        if server_id:
            out.info(f"正在对节点 {server_id} 执行全量 benchmark（单线程 + 并发梯度测试）...")
        else:
            out.info("正在对所有在线节点执行全量 benchmark（单线程 + 并发梯度测试）...")

    try:
        if server_id:
            item = c.client.benchmark_server(server_id)
            data = {"results": [item], "capacity_comparison": []}
        else:
            data = c.client.benchmark_servers()
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    results = data.get("results", [])
    capacity = data.get("capacity_comparison", [])

    if not c.quiet:
        out.success(f"Benchmark 完成: {len(results)} 个节点")

    for item in results:
        gradient = item.get("concurrency_gradient", [])
        if gradient and not c.quiet:
            sid = item.get("server_id", "")
            out.info(f"\n--- {sid} 梯度并发测试 ---")
            out.render(
                c.output_format, data=gradient,
                title=f"{sid} 并发梯度",
                columns=["并发数", "单文件RTF", "吞吐量RTF", "Wall Clock(s)", "总音频(s)"],
                rows=[
                    [str(g.get("concurrency", "")),
                     f"{g.get('per_file_rtf', 0):.4f}",
                     f"{g.get('throughput_rtf', 0):.4f}",
                     f"{g.get('wall_clock_sec', 0):.2f}",
                     f"{g.get('total_audio_sec', 0):.1f}"]
                    for g in gradient
                ],
            )

    if capacity:
        out.render(
            c.output_format, data=data, title="节点性能对比（基于吞吐量RTF）",
            columns=["server_id", "单线程RTF", "吞吐量RTF", "最优并发", "推荐并发→槽位", "吞吐速度", "相对速度"],
            rows=[
                [r.get("server_id", ""),
                 next((f"{i.get('single_rtf', '-')}" for i in results
                       if i.get("server_id") == r.get("server_id")), "-"),
                 next((f"{i.get('throughput_rtf', '-')}" for i in results
                       if i.get("server_id") == r.get("server_id")), "-"),
                 next((f"{i.get('benchmark_concurrency', '-')}" for i in results
                       if i.get("server_id") == r.get("server_id")), "-"),
                 next((f"{i.get('recommended_concurrency', '-')}" for i in results
                       if i.get("server_id") == r.get("server_id")), "-"),
                 f"{r.get('acceleration_ratio', 0):.1f}x",
                 f"{r.get('relative_speed', 0)*100:.0f}%"]
                for r in capacity
            ],
        )
    else:
        out.render(
            c.output_format, data=data, title="节点 benchmark 结果",
            columns=["server_id", "单线程RTF", "吞吐量RTF", "最优并发", "推荐并发→槽位", "样本"],
            rows=[[
                item.get("server_id", ""),
                str(item.get("single_rtf", "-") or "-"),
                str(item.get("throughput_rtf", "-") or "-"),
                str(item.get("benchmark_concurrency", "-") or "-"),
                str(item.get("recommended_concurrency", "-") or "-"),
                ", ".join(item.get("benchmark_samples", [])),
            ] for item in results],
        )


@app.command(name="update")
def update(
    ctx: typer.Context,
    server_id: str = typer.Argument(..., help="节点 ID"),
    name: Optional[str] = typer.Option(None, "--name", help="节点名称"),
    host: Optional[str] = typer.Option(None, "--host", help="主机地址"),
    port: Optional[int] = typer.Option(None, "--port", help="端口"),
    max_concurrency: Optional[int] = typer.Option(None, "--max-concurrency", help="最大并发数"),
    protocol: Optional[str] = typer.Option(None, "--protocol", help="协议版本"),
):
    """更新 ASR 节点配置。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    data = {}
    if name is not None:
        data["name"] = name
    if host is not None:
        data["host"] = host
    if port is not None:
        data["port"] = port
    if max_concurrency is not None:
        data["max_concurrency"] = max_concurrency
    if protocol is not None:
        data["protocol_version"] = protocol

    if not data:
        out.error("请至少指定一个要更新的字段")
        raise typer.Exit(1)

    try:
        result = c.client.update_server(server_id, data)
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    out.success(f"节点 {server_id} 已更新")
    out.render(c.output_format, data=result)
