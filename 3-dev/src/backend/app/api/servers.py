"""ASR server management endpoints.

All server management routes require admin authentication.
"""

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.deps import DbSession
from app.models import ServerInstance, ServerStatus
from app.schemas.server import (
    ServerBenchmarkItem,
    ConcurrencyGradientItem,
    ServerProbeResponse,
    ServerRegisterRequest,
    ServerResponse,
    ServerUpdateRequest,
)
from app.services.scheduler import ServerProfile, scheduler as global_scheduler
from app.services.server_probe import ProbeLevel, ServerCapabilities, probe_server
from app.services.server_benchmark import benchmark_server_full_with_ssl_fallback
from app.storage.repository import ServerRepository
from app.auth.token import verify_admin
from app.observability.logging import get_logger


logger = get_logger(__name__)

AdminUser = Annotated[str, Depends(verify_admin)]

router = APIRouter(prefix="/api/v1/servers", tags=["servers"])


@router.post("", response_model=ServerResponse, status_code=201)
async def register_server(body: ServerRegisterRequest, db: DbSession, admin: AdminUser):
    repo = ServerRepository(db)
    existing = await repo.get_server(body.server_id)
    if existing:
        raise HTTPException(status_code=409, detail="Server already registered")

    server_type = None
    supported_modes = None
    initial_status = ServerStatus.OFFLINE

    caps = await _probe_with_ssl_fallback(body.host, body.port, ProbeLevel.OFFLINE_LIGHT, 8.0)
    if caps.reachable:
        initial_status = ServerStatus.ONLINE
        if caps.inferred_server_type != "unknown":
            server_type = caps.inferred_server_type
        modes = _extract_modes(caps)
        if modes:
            supported_modes = ",".join(modes)
        logger.info("server_probe_on_register", server_id=body.server_id,
                    reachable=caps.reachable, responsive=caps.responsive,
                    inferred_type=caps.inferred_server_type)
    else:
        logger.warning("server_probe_unreachable", server_id=body.server_id,
                        error=caps.error, status="registering as OFFLINE")

    server = ServerInstance(
        server_id=body.server_id, name=body.name, host=body.host, port=body.port,
        protocol_version=body.protocol_version, server_type=server_type,
        supported_modes=supported_modes, max_concurrency=body.max_concurrency,
        status=initial_status, labels_json=json.dumps(body.labels) if body.labels else None,
    )
    await repo.register_server(server)
    logger.info("server_registered", server_id=body.server_id, host=body.host, by=admin)

    if body.run_benchmark and initial_status == ServerStatus.ONLINE:
        await db.commit()
        host, port, sid = server.host, server.port, body.server_id
        server_data = ServerResponse.model_validate(server).model_dump(mode="json")

        async def generate():
            from app.storage.database import async_session_factory

            progress_queue: asyncio.Queue[dict] = asyncio.Queue()

            yield json.dumps({
                "type": "server_registered",
                "server_id": sid,
                "data": server_data,
            }, ensure_ascii=False) + "\n"

            async def on_progress(event: dict):
                event["server_id"] = sid
                await progress_queue.put(event)

            async def run_benchmark():
                logger.info("server_register_benchmark_start", server_id=sid)
                try:
                    bench = await benchmark_server_full_with_ssl_fallback(
                        host, port, timeout=900.0,
                        progress_callback=on_progress,
                    )
                except Exception as exc:
                    logger.warning("server_register_benchmark_failed",
                                   server_id=sid, error=str(exc))
                    await progress_queue.put({
                        "type": "benchmark_error",
                        "server_id": sid,
                        "error": str(exc),
                    })
                    return

                async with async_session_factory() as session:
                    s_repo = ServerRepository(session)
                    srv = await s_repo.get_server(sid)
                    if srv:
                        if bench.reachable:
                            _apply_benchmark_result(srv, bench)
                        else:
                            srv.status = ServerStatus.OFFLINE
                        await session.commit()

                if bench.reachable:
                    logger.info("server_register_benchmark_done",
                                server_id=sid,
                                single_rtf=bench.single_rtf,
                                throughput_rtf=bench.throughput_rtf)
                else:
                    logger.warning("server_register_benchmark_unreachable",
                                   server_id=sid, error=bench.error)

                await progress_queue.put({
                    "type": "benchmark_result",
                    "server_id": sid,
                    "data": _build_benchmark_item(sid, bench).model_dump(mode="json"),
                })

            task = asyncio.create_task(run_benchmark())

            while True:
                try:
                    event = await asyncio.wait_for(progress_queue.get(), timeout=300)
                except asyncio.TimeoutError:
                    yield json.dumps({"type": "keepalive"}, ensure_ascii=False) + "\n"
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in ("benchmark_result", "benchmark_error"):
                    break

            await task

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            status_code=201,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return ServerResponse.model_validate(server)


@router.get("", response_model=list[ServerResponse])
async def list_servers(db: DbSession, admin: AdminUser):
    repo = ServerRepository(db)
    servers = await repo.list_all_servers()
    return [ServerResponse.model_validate(s) for s in servers]


@router.post("/benchmark")
async def benchmark_all_servers(db: DbSession, admin: AdminUser):
    """Run full benchmark for all ONLINE servers.

    Returns an NDJSON stream. Each progress event carries a ``server_id``
    field. The final event has type ``all_complete`` with aggregated results
    and capacity comparison.
    """
    repo = ServerRepository(db)
    all_servers = await repo.list_all_servers()
    online_servers = [s for s in all_servers if s.status == ServerStatus.ONLINE]

    if not online_servers:
        raise HTTPException(status_code=422, detail="No online servers to benchmark")

    server_list = [(s.server_id, s.host, s.port) for s in online_servers]

    async def generate():
        from app.storage.database import async_session_factory

        progress_queue: asyncio.Queue[dict] = asyncio.Queue()
        bench_results: dict[str, ServerBenchmarkItem] = {}
        completed: set[str] = set()
        total = len(server_list)

        yield json.dumps({
            "type": "all_benchmark_start",
            "server_ids": [sid for sid, _, _ in server_list],
            "total_servers": total,
        }, ensure_ascii=False) + "\n"

        async def bench_one(sid: str, host: str, port: int):
            async def on_progress(event: dict):
                event["server_id"] = sid
                await progress_queue.put(event)

            try:
                bench = await benchmark_server_full_with_ssl_fallback(
                    host, port, timeout=900.0,
                    progress_callback=on_progress,
                )
            except (FileNotFoundError, ValueError) as exc:
                await progress_queue.put({
                    "type": "server_error",
                    "server_id": sid,
                    "error": f"Benchmark 配置错误: {exc}",
                })
                return
            except Exception as exc:
                await progress_queue.put({
                    "type": "server_error",
                    "server_id": sid,
                    "error": str(exc),
                })
                return

            async with async_session_factory() as session:
                s_repo = ServerRepository(session)
                srv = await s_repo.get_server(sid)
                if srv:
                    if not bench.reachable:
                        srv.status = ServerStatus.OFFLINE
                        logger.warning("benchmark_server_unreachable",
                                       server_id=sid, error=bench.error)
                    else:
                        _apply_benchmark_result(srv, bench)
                    await session.commit()

            item = _build_benchmark_item(sid, bench)
            bench_results[sid] = item
            await progress_queue.put({
                "type": "server_benchmark_done",
                "server_id": sid,
                "completed": len(bench_results),
                "total": total,
                "data": item.model_dump(mode="json"),
            })

        tasks = [asyncio.create_task(bench_one(sid, h, p))
                 for sid, h, p in server_list]

        while len(completed) < total:
            try:
                event = await asyncio.wait_for(progress_queue.get(), timeout=300)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "keepalive"}, ensure_ascii=False) + "\n"
                continue
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event["type"] in ("server_benchmark_done", "server_error"):
                completed.add(event["server_id"])

        await asyncio.gather(*tasks, return_exceptions=True)

        async with async_session_factory() as session:
            s_repo = ServerRepository(session)
            all_srvs = await s_repo.list_all_servers()
            still_online = [s for s in all_srvs if s.status == ServerStatus.ONLINE]

        profiles = [
            ServerProfile(
                server_id=s.server_id, host=s.host, port=s.port,
                max_concurrency=s.max_concurrency,
                rtf_baseline=s.rtf_baseline,
                throughput_rtf=s.throughput_rtf,
                penalty_factor=s.penalty_factor,
            )
            for s in still_online
        ]
        comparison = global_scheduler.compare_server_capacity(profiles)

        results_list = [item.model_dump(mode="json") for item in bench_results.values()]
        logger.info("benchmark_all_complete",
                    servers=list(bench_results.keys()),
                    comparison=comparison)

        yield json.dumps({
            "type": "all_complete",
            "data": {
                "results": results_list,
                "capacity_comparison": comparison,
            },
        }, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{server_id}/benchmark")
async def benchmark_server_endpoint(server_id: str, db: DbSession, admin: AdminUser):
    """Run full benchmark (single + concurrent) for one registered server.

    Returns an NDJSON stream with real-time progress events. The final event
    has type ``benchmark_result`` (success) or ``benchmark_error`` (failure).
    """
    repo = ServerRepository(db)
    server = await repo.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    host, port = server.host, server.port

    async def generate():
        from app.storage.database import async_session_factory

        progress_queue: asyncio.Queue[dict] = asyncio.Queue()

        async def on_progress(event: dict):
            event["server_id"] = server_id
            await progress_queue.put(event)

        async def run_benchmark():
            try:
                bench = await benchmark_server_full_with_ssl_fallback(
                    host, port, timeout=900.0,
                    progress_callback=on_progress,
                )
            except (FileNotFoundError, ValueError) as exc:
                await progress_queue.put({
                    "type": "benchmark_error",
                    "server_id": server_id,
                    "error": f"Benchmark 配置错误（非服务器连通性问题）: {exc}",
                })
                return
            except Exception as exc:
                await progress_queue.put({
                    "type": "benchmark_error",
                    "server_id": server_id,
                    "error": str(exc),
                })
                return

            async with async_session_factory() as session:
                s_repo = ServerRepository(session)
                srv = await s_repo.get_server(server_id)
                if srv:
                    srv.status = ServerStatus.ONLINE if bench.reachable else ServerStatus.OFFLINE
                    if bench.reachable:
                        _apply_benchmark_result(srv, bench)
                    await session.commit()

            await progress_queue.put({
                "type": "benchmark_result",
                "server_id": server_id,
                "data": _build_benchmark_item(server_id, bench).model_dump(mode="json"),
            })

        task = asyncio.create_task(run_benchmark())

        while True:
            try:
                event = await asyncio.wait_for(progress_queue.get(), timeout=300)
            except asyncio.TimeoutError:
                yield json.dumps({"type": "keepalive"}, ensure_ascii=False) + "\n"
                continue
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event["type"] in ("benchmark_result", "benchmark_error"):
                break

        await task

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{server_id}/probe", response_model=ServerProbeResponse)
async def probe_server_endpoint(
    server_id: str,
    db: DbSession,
    admin: AdminUser,
    level: str = Query(
        "offline_light",
        pattern="^(connect_only|offline_light|twopass_full)$",
        description="probe 仅用于 WebSocket 连通性与能力探测，不执行 benchmark。",
    ),
):
    """Probe a registered server for connectivity and capabilities only."""
    repo = ServerRepository(db)
    server = await repo.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    probe_level_map = {
        "connect_only": ProbeLevel.CONNECT_ONLY,
        "offline_light": ProbeLevel.OFFLINE_LIGHT,
        "twopass_full": ProbeLevel.TWOPASS_FULL,
    }
    timeout = 12.0
    caps = await _probe_with_ssl_fallback(
        server.host, server.port, probe_level_map[level], timeout,
    )

    if caps.reachable and caps.inferred_server_type != "unknown":
        server.server_type = caps.inferred_server_type
    modes = _extract_modes(caps)
    server.supported_modes = ",".join(modes) if modes else None

    if caps.reachable:
        server.status = ServerStatus.ONLINE
    else:
        server.status = ServerStatus.OFFLINE

    await db.commit()

    logger.info("server_probed", server_id=server_id, reachable=caps.reachable,
                inferred_type=caps.inferred_server_type)

    return ServerProbeResponse(
        server_id=server_id,
        **caps.to_dict(),
    )


@router.patch("/{server_id}", response_model=ServerResponse)
async def update_server(server_id: str, body: ServerUpdateRequest, db: DbSession, admin: AdminUser):
    """Update a registered server's configuration."""
    repo = ServerRepository(db)
    server = await repo.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    import json as _json
    updates = body.model_dump(exclude_unset=True)

    if "labels" in updates:
        labels = updates.pop("labels")
        server.labels_json = _json.dumps(labels) if labels else None
    for field, value in updates.items():
        setattr(server, field, value)

    await db.commit()
    await db.refresh(server)
    logger.info("server_updated", server_id=server_id, fields=list(updates.keys()), by=admin)
    return ServerResponse.model_validate(server)


@router.delete("/{server_id}", status_code=204)
async def delete_server(server_id: str, db: DbSession, admin: AdminUser):
    from sqlalchemy import select as _sel, func as _fn
    from app.models import Task, TaskStatus as _TS

    active_count_stmt = (
        _sel(_fn.count())
        .select_from(Task)
        .where(
            Task.assigned_server_id == server_id,
            Task.status.in_([_TS.DISPATCHED, _TS.TRANSCRIBING]),
        )
    )
    active_count = (await db.execute(active_count_stmt)).scalar() or 0
    if active_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Server {server_id} still has {active_count} active task(s) "
                   "(DISPATCHED/TRANSCRIBING). Cancel or wait for them to finish before deleting.",
        )

    bound_stmt = (
        _sel(Task)
        .where(Task.assigned_server_id == server_id)
        .limit(500)
    )
    bound_tasks = (await db.execute(bound_stmt)).scalars().all()
    for task in bound_tasks:
        task.assigned_server_id = None

    repo = ServerRepository(db)
    deleted = await repo.delete_server(server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Server not found")
    logger.info("server_deleted", server_id=server_id, unbound_tasks=len(bound_tasks), by=admin)


def _apply_benchmark_result(
    server: ServerInstance,
    bench,
) -> None:
    """Write benchmark metrics to server model fields.

    RTF baselines are exclusively set here (benchmark owns them).
    max_concurrency is set to the degradation-detected recommended value
    so that scheduling matches the server's true concurrent capacity.

    When the gradient terminated early due to errors (gradient_complete=False),
    only allow max_concurrency to stay the same or increase — never downgrade
    based on incomplete evidence from a transient failure.
    """
    if bench.single_rtf is not None:
        server.rtf_baseline = bench.single_rtf
    if bench.throughput_rtf is not None:
        server.throughput_rtf = bench.throughput_rtf
    if bench.benchmark_concurrency is not None:
        server.benchmark_concurrency = bench.benchmark_concurrency
    if bench.recommended_concurrency is not None:
        old_mc = server.max_concurrency
        if not bench.gradient_complete and bench.recommended_concurrency < old_mc:
            logger.warning(
                "benchmark_incomplete_gradient_skip_downgrade",
                server_id=server.server_id,
                current=old_mc,
                would_be=bench.recommended_concurrency,
                reason="gradient terminated early by error; keeping current max_concurrency",
            )
        else:
            server.max_concurrency = bench.recommended_concurrency
            if old_mc != bench.recommended_concurrency:
                logger.info(
                    "benchmark_auto_adjust_concurrency",
                    server_id=server.server_id,
                    old=old_mc,
                    new=bench.recommended_concurrency,
                    reason="degradation_detection" if bench.gradient_complete else "partial_gradient_upgrade",
                )


def _build_benchmark_item(server_id: str, bench) -> ServerBenchmarkItem:
    """Convert a ServerBenchmarkResult to the API response schema."""
    item_data = bench.to_dict()
    gradient_raw = item_data.pop("concurrency_gradient", [])
    return ServerBenchmarkItem(
        server_id=server_id,
        concurrency_gradient=[ConcurrencyGradientItem(**g) for g in gradient_raw],
        **item_data,
    )


def _extract_modes(caps) -> list[str]:
    modes = []
    if caps.supports_offline:
        modes.append("offline")
    if caps.supports_2pass:
        modes.append("2pass")
    if caps.supports_online:
        modes.append("online")
    return modes


async def _probe_with_ssl_fallback(
    host: str, port: int, level: ProbeLevel, timeout: float,
):
    """Probe with wss first; on SSL/connection error, retry with plain ws."""
    try:
        caps = await probe_server(host=host, port=port, use_ssl=True, level=level, timeout=timeout)
        if caps.reachable:
            return caps
    except Exception as e:
        logger.warning("probe_wss_exception", host=host, port=port, error=str(e))
        caps = None

    err_msg = (caps.error or "") if caps else ""
    is_ssl_error = any(k in err_msg.lower() for k in ("ssl", "tls", "certificate"))
    is_conn_error = any(k in err_msg.lower() for k in ("refused", "timeout", "network", "websocket", "http response"))
    if is_ssl_error or is_conn_error or caps is None:
        logger.info("probe_retry_plain_ws", host=host, port=port, original_error=err_msg)
        try:
            ws_caps = await probe_server(host=host, port=port, use_ssl=False, level=level, timeout=timeout)
            return ws_caps
        except Exception as e:
            logger.warning("probe_ws_also_failed", host=host, port=port, error=str(e))

    return caps if caps else ServerCapabilities(error="probe failed")
