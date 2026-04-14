"""Alertmanager webhook receiver endpoint."""

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

_ALERT_WEBHOOK_SECRET = os.environ.get("ASR_ALERT_WEBHOOK_SECRET", "")


@router.post("/alert-webhook")
async def receive_alertmanager_webhook(request: Request) -> dict[str, Any]:
    """Receive and log Alertmanager webhook notifications.

    Authenticated via a dedicated webhook secret (ASR_ALERT_WEBHOOK_SECRET),
    independent of admin token auth, so that Alertmanager can POST without
    an X-API-Key header.
    """
    if _ALERT_WEBHOOK_SECRET:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {_ALERT_WEBHOOK_SECRET}":
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()

    status = payload.get("status", "unknown")
    alerts = payload.get("alerts", [])

    for alert in alerts:
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        logger.info(
            "alertmanager_notification",
            status=alert.get("status", status),
            alertname=labels.get("alertname", "unknown"),
            severity=labels.get("severity", "unknown"),
            summary=annotations.get("summary", ""),
            description=annotations.get("description", ""),
            starts_at=alert.get("startsAt", ""),
            ends_at=alert.get("endsAt", ""),
        )

    return {"status": "ok", "received_alerts": len(alerts)}
