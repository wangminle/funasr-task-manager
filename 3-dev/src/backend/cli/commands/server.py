"""Server management commands: list, register, delete, probe, benchmark, update."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli.api_client import APIError
from cli import output as out

logger = logging.getLogger(__name__)

BENCHMARK_REPEAT_WINDOW_MINUTES = 10


def _detect_archive_dir() -> Path | None:
    """Locate benchmark archive directory via ASR_PROJECT_ROOT or git root."""
    env_root = os.environ.get("ASR_PROJECT_ROOT")
    if env_root:
        return Path(env_root) / "4-tests" / "batch-testing" / "outputs" / "benchmark"
    # Fallback: walk up from this file to find repo root (has .git)
    candidate = Path(__file__).resolve()
    for _ in range(10):
        candidate = candidate.parent
        if (candidate / ".git").exists():
            return candidate / "4-tests" / "batch-testing" / "outputs" / "benchmark"
    return None


def _last_benchmark_age_minutes() -> float | None:
    """Return minutes since the most recent benchmark archive, or None if unknown."""
    archive_dir = _detect_archive_dir()
    if archive_dir is None or not archive_dir.exists():
        return None
    archives = sorted(archive_dir.glob("benchmark-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not archives:
        return None
    last_mtime = archives[0].stat().st_mtime
    age_sec = datetime.now(timezone.utc).timestamp() - last_mtime
    return age_sec / 60.0


def _archive_benchmark(results: list[dict], capacity: list[dict]) -> Path | None:
    """Write benchmark results to JSON archive file. Returns archive dir on success."""
    archive_dir = _detect_archive_dir()
    if archive_dir is None:
        logger.debug("benchmark_archive_skip: cannot detect project root")
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    server_ids = [r.get("server_id", "unknown") for r in results]
    tag = "_".join(server_ids) if len(server_ids) <= 3 else f"{len(server_ids)}servers"
    filename = f"benchmark-{tag}-{ts}.json"
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "capacity_comparison": capacity,
    }
    out_path = archive_dir / filename
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("benchmark_archived: %s", out_path)
    return archive_dir

app = typer.Typer()


def _print_benchmark_event(event: dict) -> None:
    """Format and print a single NDJSON benchmark progress event."""
    evt = event.get("type", "")
    sid = event.get("server_id", "")
    tag = f"[{sid}] " if sid else ""

    if evt == "benchmark_start":
        samples = ", ".join(event.get("samples", []))
        out.info(f"{tag}Benchmark 开始 (样本: {samples})")
    elif evt == "phase_start":
        out.info(f"{tag}Phase {event.get('phase')}: {event.get('description', '')}")
    elif evt == "phase_progress":
        out.info(f"{tag}  采样 {event.get('rep')}/{event.get('total_reps')}: RTF={event.get('rtf')}")
    elif evt == "phase_complete":
        out.success(f"{tag}Phase {event.get('phase')} 完成: single_rtf={event.get('single_rtf')}")
    elif evt == "gradient_start":
        n = event.get("concurrency")
        idx = event.get("level_index")
        total = event.get("total_levels")
        out.info(f"{tag}  梯度 N={n} ({idx}/{total})...")
    elif evt == "gradient_complete":
        out.success(
            f"{tag}  ✓ N={event.get('concurrency')}: "
            f"throughput_rtf={event.get('throughput_rtf')}, "
            f"wall={event.get('wall_clock_sec')}s"
        )
    elif evt == "gradient_error":
        out.error(f"{tag}  ✗ N={event.get('concurrency')}: {event.get('error', '')}")
    elif evt == "benchmark_complete":
        out.success(
            f"{tag}推荐并发={event.get('recommended_concurrency')}, "
            f"single_rtf={event.get('single_rtf')}, "
            f"throughput_rtf={event.get('throughput_rtf')}"
        )
    elif evt == "ssl_fallback":
        out.info(f"{tag}WSS 连接失败，回退到 WS 重试...")
    elif evt == "all_benchmark_start":
        sids = event.get("server_ids", [])
        out.info(f"即将对 {len(sids)} 个节点执行 benchmark: {', '.join(sids)}")
    elif evt == "server_benchmark_done":
        out.success(f"[{event.get('server_id')}] 完成 ({event.get('completed')}/{event.get('total')})")
    elif evt == "server_error":
        out.error(f"[{event.get('server_id')}] 错误: {event.get('error', '')}")


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
    run_benchmark: bool = typer.Option(False, "--benchmark", help="注册后立即执行完整 Benchmark 测速"),
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
        "run_benchmark": run_benchmark,
    }

    if run_benchmark:
        if not c.quiet:
            out.info("注册后将自动执行 Benchmark，耗时较长请耐心等待...")
        bench_failed = False
        try:
            server_result = None
            bench_data = None
            for event in c.client.register_server_stream(data):
                evt_type = event.get("type")
                if evt_type == "server_registered":
                    server_result = event.get("data", {})
                    if not c.quiet:
                        out.success(f"节点注册成功: {server_id}")
                elif evt_type == "benchmark_result":
                    bench_data = event.get("data", {})
                elif evt_type == "benchmark_error":
                    out.error(f"Benchmark 失败: {event.get('error', '')}")
                    bench_failed = True
                elif not c.quiet:
                    _print_benchmark_event(event)
        except APIError as e:
            out.error(e.detail)
            raise typer.Exit(1)

        if bench_data:
            rtf = bench_data.get("single_rtf")
            tp = bench_data.get("throughput_rtf")
            rec = bench_data.get("recommended_concurrency")
            out.success(f"Benchmark 完成: single_rtf={rtf}, throughput_rtf={tp}, 推荐并发={rec}")
        out.render(c.output_format, data=server_result or {})
        if bench_failed:
            raise typer.Exit(1)
    else:
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
    force: bool = typer.Option(False, "--force", "-f", help="跳过繁忙/离线等前置安全检查"),
):
    """使用公开 benchmark 样本对单个或全部在线节点执行真实 RTF 基准测试。"""
    from cli.main import get_ctx
    c = get_ctx(ctx)

    # --- 前置安全检查 ---
    if not force:
        try:
            stats = c.client.stats(global_stats=True)
            if isinstance(stats, dict):
                slots_used = stats.get("slots_used", 0)
                queue_depth = stats.get("queue_depth", 0)
                if slots_used > 0 or queue_depth > 0:
                    out.error(
                        f"系统繁忙 (slots_used={slots_used}, queue_depth={queue_depth})，"
                        f"benchmark 会影响正在进行的任务。请等待队列空闲后重试，"
                        f"或使用 --force 强制执行。"
                    )
                    raise typer.Exit(1)
        except typer.Exit:
            raise
        except Exception:
            pass  # stats 不可用时不阻塞 benchmark

        try:
            age = _last_benchmark_age_minutes()
            if age is not None and age < BENCHMARK_REPEAT_WINDOW_MINUTES:
                out.error(
                    f"距上次 benchmark 仅 {age:.1f} 分钟（窗口 {BENCHMARK_REPEAT_WINDOW_MINUTES} 分钟），"
                    f"跳过重复执行。使用 --force 强制执行。"
                )
                raise typer.Exit(1)
        except typer.Exit:
            raise
        except Exception:
            pass

        if server_id:
            try:
                servers = c.client.list_servers()
                if isinstance(servers, list):
                    target = next((s for s in servers
                                   if isinstance(s, dict) and s.get("server_id") == server_id), None)
                    if target and target.get("status") == "OFFLINE":
                        out.error(
                            f"节点 {server_id} 当前状态为 OFFLINE，请先执行 "
                            f"'server probe {server_id}' 确认可达后再 benchmark。"
                        )
                        raise typer.Exit(1)
            except typer.Exit:
                raise
            except Exception:
                pass

    if not c.quiet:
        if server_id:
            out.info(f"正在对节点 {server_id} 执行全量 benchmark（单线程 + 并发梯度测试）...")
        else:
            out.info("正在对所有在线节点执行全量 benchmark（单线程 + 并发梯度测试）...")

    results: list[dict] = []
    capacity: list[dict] = []
    error_count = 0

    try:
        if server_id:
            for event in c.client.benchmark_server_stream(server_id):
                evt_type = event.get("type")
                if not c.quiet and evt_type not in ("benchmark_result", "benchmark_error"):
                    _print_benchmark_event(event)
                if evt_type == "benchmark_result":
                    results.append(event.get("data", {}))
                elif evt_type == "benchmark_error":
                    out.error(event.get("error", "benchmark failed"))
                    raise typer.Exit(1)
        else:
            for event in c.client.benchmark_servers_stream():
                evt_type = event.get("type")
                if not c.quiet and evt_type not in (
                    "all_complete", "server_benchmark_done", "server_error",
                    "benchmark_result",
                ):
                    _print_benchmark_event(event)
                if evt_type == "server_benchmark_done":
                    results.append(event.get("data", {}))
                    if not c.quiet:
                        _print_benchmark_event(event)
                elif evt_type == "server_error":
                    error_count += 1
                    if not c.quiet:
                        _print_benchmark_event(event)
                elif evt_type == "all_complete":
                    data = event.get("data", {})
                    results = data.get("results", results)
                    capacity = data.get("capacity_comparison", [])
    except APIError as e:
        out.error(e.detail)
        raise typer.Exit(1)

    if not results and error_count > 0:
        out.error(f"所有 {error_count} 个节点 Benchmark 均失败")
        raise typer.Exit(1)

    if not c.quiet:
        msg = f"\nBenchmark 完成: {len(results)} 个节点"
        if error_count > 0:
            msg += f" ({error_count} 个失败)"
        out.success(msg)

    for item in results:
        gradient = item.get("concurrency_gradient", [])
        if gradient and not c.quiet:
            sid = item.get("server_id", "")
            out.info(f"\n--- {sid} 梯度并发测试 ---")
            out.render(
                c.output_format, data=gradient,
                title=f"{sid} 并发梯度",
                columns=[
                    "并发", "吞吐RTF", "服务端RTF",
                    "上传(ms)", "等待(ms)", "离散(ms)",
                    "RTT(ms)", "Wall(s)",
                ],
                rows=[
                    [str(g.get("concurrency", "")),
                     f"{g.get('throughput_rtf', 0):.4f}",
                     f"{g.get('server_throughput_rtf', 0):.4f}",
                     f"{g.get('avg_upload_ms', 0):.0f}",
                     f"{g.get('concurrent_post_upload_ms', 0):.0f}",
                     f"{g.get('upload_spread_ms', 0):.0f}",
                     f"{g.get('ping_rtt_ms', 0) or 0:.1f}",
                     f"{g.get('wall_clock_sec', 0):.2f}"]
                    for g in gradient
                ],
            )

    data = {"results": results, "capacity_comparison": capacity}
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

    if results:
        try:
            archived_to = _archive_benchmark(results, capacity)
            if not c.quiet and archived_to:
                out.info(f"结果已归档到 {archived_to}/")
        except Exception as exc:
            logger.warning("benchmark_archive_failed: %s", exc)


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
