"""ASR server management endpoints."""

import json

from fastapi import APIRouter, HTTPException

from app.deps import DbSession
from app.models import ServerInstance, ServerStatus
from app.schemas.server import ServerRegisterRequest, ServerResponse
from app.storage.repository import ServerRepository
from app.observability.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/servers", tags=["servers"])


@router.post("", response_model=ServerResponse, status_code=201)
async def register_server(body: ServerRegisterRequest, db: DbSession):
    repo = ServerRepository(db)
    existing = await repo.get_server(body.server_id)
    if existing:
        raise HTTPException(status_code=409, detail="Server already registered")
    server = ServerInstance(
        server_id=body.server_id, name=body.name, host=body.host, port=body.port,
        protocol_version=body.protocol_version, max_concurrency=body.max_concurrency,
        status=ServerStatus.ONLINE, labels_json=json.dumps(body.labels) if body.labels else None,
    )
    await repo.register_server(server)
    logger.info("server_registered", server_id=body.server_id, host=body.host)
    return ServerResponse.model_validate(server)


@router.get("", response_model=list[ServerResponse])
async def list_servers(db: DbSession):
    repo = ServerRepository(db)
    servers = await repo.list_all_servers()
    return [ServerResponse.model_validate(s) for s in servers]


@router.delete("/{server_id}", status_code=204)
async def delete_server(server_id: str, db: DbSession):
    repo = ServerRepository(db)
    deleted = await repo.delete_server(server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Server not found")
    logger.info("server_deleted", server_id=server_id)
