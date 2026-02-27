"""Outbox callback delivery service.

Implements the transactional outbox pattern:
1. When task status changes, write a callback record in the same DB transaction
2. Background worker scans PENDING records and delivers them via HTTP POST
3. Successful delivery → status=SENT; Failed → retry with exponential backoff
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
from ulid import ULID

from app.models.callback_outbox import CallbackOutbox, OutboxStatus
from app.fault.retry import calculate_delay
from app.observability.logging import get_logger

logger = get_logger(__name__)

MAX_CALLBACK_RETRIES = 5
CALLBACK_TIMEOUT = 10.0


def generate_hmac_signature(payload: str, secret: str) -> str:
    """Generate HMAC-SHA256 signature for webhook verification."""
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def build_callback_payload(
    task_id: str,
    event_id: str,
    status: str,
    progress: float = 0.0,
    result_path: str | None = None,
    error_message: str | None = None,
) -> str:
    """Build JSON payload for callback delivery."""
    payload = {
        "event_id": event_id,
        "task_id": task_id,
        "status": status,
        "progress": progress,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if result_path:
        payload["result_path"] = result_path
    if error_message:
        payload["error_message"] = error_message
    return json.dumps(payload, ensure_ascii=False)


def create_outbox_record(
    task_id: str,
    event_id: str,
    callback_url: str,
    status: str,
    progress: float = 0.0,
    result_path: str | None = None,
    error_message: str | None = None,
) -> CallbackOutbox:
    """Create an outbox record for later delivery."""
    payload = build_callback_payload(task_id, event_id, status, progress, result_path, error_message)
    return CallbackOutbox(
        outbox_id=str(ULID()),
        task_id=task_id,
        event_id=event_id,
        callback_url=callback_url,
        payload_json=payload,
        status=OutboxStatus.PENDING,
    )


async def deliver_callback(
    outbox: CallbackOutbox,
    secret: str | None = None,
) -> bool:
    """Attempt to deliver a callback via HTTP POST.
    
    Returns True if delivery succeeded, False otherwise.
    """
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = generate_hmac_signature(outbox.payload_json, secret)
        headers["X-Webhook-Signature"] = sig
    headers["X-Event-ID"] = outbox.event_id

    try:
        async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT) as client:
            resp = await client.post(
                outbox.callback_url,
                content=outbox.payload_json,
                headers=headers,
            )
        if 200 <= resp.status_code < 300:
            outbox.status = OutboxStatus.SENT
            outbox.sent_at = datetime.now(timezone.utc)
            logger.info("callback_delivered", outbox_id=outbox.outbox_id, task_id=outbox.task_id, status_code=resp.status_code)
            return True
        else:
            outbox.retry_count += 1
            outbox.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if outbox.retry_count >= MAX_CALLBACK_RETRIES:
                outbox.status = OutboxStatus.FAILED
            logger.warning("callback_delivery_failed", outbox_id=outbox.outbox_id, status_code=resp.status_code, retry=outbox.retry_count)
            return False
    except Exception as e:
        outbox.retry_count += 1
        outbox.last_error = str(e)
        if outbox.retry_count >= MAX_CALLBACK_RETRIES:
            outbox.status = OutboxStatus.FAILED
        logger.error("callback_delivery_error", outbox_id=outbox.outbox_id, error=str(e), retry=outbox.retry_count)
        return False


def get_retry_delay(retry_count: int) -> float:
    """Get delay before next retry attempt."""
    return calculate_delay(retry_count, base_delay=2.0, max_delay=60.0)
