"""ASR server management endpoints.

All server management routes require admin authentication.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import DbSession
from app.models import ServerInstance, ServerStatus
from app.schemas.server import (
    ServerBenchmarkResponse,
    ServerBenchmarkItem,
    ServerCapacityItem,
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
    return ServerResponse.model_validate(server)


@router.get("", response_model=list[ServerResponse])
async def list_servers(db: DbSession, admin: AdminUser):
    repo = ServerRepository(db)
    servers = await repo.list_all_servers()
    return [ServerResponse.model_validate(s) for s in servers]


@router.post("/benchmark", response_model=ServerBenchmarkResponse)
async def benchmark_all_servers(db: DbSession, admin: AdminUser):
    """Run full benchmark (single-thread RTF + concurrent throughput RTF) for all ONLINE servers."""
    repo = ServerRepository(db)
    all_servers = await repo.list_all_servers()
    online_servers = [s for s in all_servers if s.status == ServerStatus.ONLINE]

    if not online_servers:
        raise HTTPException(status_code=422, detail="No online servers to benchmark")

    results: list[ServerBenchmarkItem] = []
    for server in online_servers:
        try:
            bench = await benchmark_server_full_with_ssl_fallback(
                server.host, server.port,
                max_concurrency=server.max_concurrency,
                timeout=900.0,
            )
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Benchmark 配置错误（非服务器连通性问题）: {exc}",
            )

        if not bench.reachable:
            server.status = ServerStatus.OFFLINE
            logger.warning("benchmark_server_unreachable",
                           server_id=server.server_id, error=bench.error)
        else:
            if bench.single_rtf is not None:
                server.rtf_baseline = bench.single_rtf
            if bench.throughput_rtf is not None:
                server.throughput_rtf = bench.throughput_rtf
            if bench.benchmark_concurrency is not None:
                server.benchmark_concurrency = bench.benchmark_concurrency

        item_data = bench.to_dict()
        gradient_raw = item_data.pop("concurrency_gradient", [])
        results.append(ServerBenchmarkItem(
            server_id=server.server_id,
            concurrency_gradient=[ConcurrencyGradientItem(**g) for g in gradient_raw],
            **item_data,
        ))

    await db.commit()

    still_online = [s for s in online_servers if s.status == ServerStatus.ONLINE]
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

    logger.info("benchmark_all_complete",
                servers=[r.server_id for r in results],
                comparison=comparison)

    return ServerBenchmarkResponse(
        results=results,
        capacity_comparison=[ServerCapacityItem(**c) for c in comparison],
    )


@router.post("/{server_id}/benchmark", response_model=ServerBenchmarkItem)
async def benchmark_server_endpoint(server_id: str, db: DbSession, admin: AdminUser):
    """Run full benchmark (single + concurrent) for one registered server."""
    repo = ServerRepository(db)
    server = await repo.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        bench = await benchmark_server_full_with_ssl_fallback(
            server.host, server.port,
            max_concurrency=server.max_concurrency,
            timeout=900.0,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Benchmark 配置错误（非服务器连通性问题）: {exc}",
        )

    server.status = ServerStatus.ONLINE if bench.reachable else ServerStatus.OFFLINE
    if bench.single_rtf is not None:
        server.rtf_baseline = bench.single_rtf
    if bench.throughput_rtf is not None:
        server.throughput_rtf = bench.throughput_rtf
    if bench.benchmark_concurrency is not None:
        server.benchmark_concurrency = bench.benchmark_concurrency
    await db.commit()

    item_data = bench.to_dict()
    gradient_raw = item_data.pop("concurrency_gradient", [])
    return ServerBenchmarkItem(
        server_id=server_id,
        concurrency_gradient=[ConcurrencyGradientItem(**g) for g in gradient_raw],
        **item_data,
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
