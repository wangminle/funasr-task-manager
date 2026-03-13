"""Alertmanager webhook receiver endpoint."""

from typing import Any

from fastapi import APIRouter, Request

from app.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])


@router.post("/alert-webhook")
async def receive_alertmanager_webhook(request: Request) -> dict[str, Any]:
    """Receive and log Alertmanager webhook notifications.

    Alertmanager posts JSON payloads here when alerts fire or resolve.
    This handler logs them for observability; extend it to forward
    to messaging systems (DingTalk, WeCom, Slack, etc.) as needed.
    """
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
