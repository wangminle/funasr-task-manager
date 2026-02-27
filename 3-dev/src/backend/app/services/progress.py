"""Two-layer progress calculation and management."""

from datetime import datetime, timezone

from app.models.task import STATUS_PROGRESS_RANGES, TaskStatus
from app.observability.logging import get_logger

logger = get_logger(__name__)


def calculate_progress(status: str, started_at: datetime | None = None, duration_sec: float | None = None, rtf_p90: float = 0.3) -> float:
    try:
        task_status = TaskStatus(status)
    except ValueError:
        return 0.0
    lo, hi = STATUS_PROGRESS_RANGES[task_status]
    if task_status != TaskStatus.TRANSCRIBING:
        return lo
    if started_at is None or duration_sec is None or duration_sec <= 0:
        return lo
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed = (now - started_at).total_seconds()
    estimated_total = duration_sec * rtf_p90
    if estimated_total <= 0:
        return lo
    fraction = min(elapsed / estimated_total, 1.0)
    progress = lo + fraction * (hi - lo)
    return min(progress, hi)


def calculate_eta(status: str, started_at: datetime | None = None, duration_sec: float | None = None, rtf_p90: float = 0.3) -> int | None:
    try:
        task_status = TaskStatus(status)
    except ValueError:
        return None
    if task_status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED):
        return 0
    if task_status == TaskStatus.TRANSCRIBING and started_at and duration_sec:
        now = datetime.now(timezone.utc)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = (now - started_at).total_seconds()
        estimated_total = duration_sec * rtf_p90
        remaining = max(estimated_total - elapsed, 0)
        return int(remaining)
    if duration_sec:
        overhead = 5
        return int(duration_sec * rtf_p90 + overhead)
    return None


def format_progress_message(status: str, progress: float, queue_position: int = 0) -> str:
    messages = {
        TaskStatus.PENDING: "等待处理",
        TaskStatus.PREPROCESSING: "预处理中（元信息提取/格式转换）",
        TaskStatus.QUEUED: f"排队中（第{queue_position}位）" if queue_position > 0 else "排队中",
        TaskStatus.DISPATCHED: "已分配服务器，准备开始转写",
        TaskStatus.TRANSCRIBING: f"转写中（{progress:.0%}）",
        TaskStatus.SUCCEEDED: "转写完成",
        TaskStatus.FAILED: "转写失败",
        TaskStatus.CANCELED: "已取消",
    }
    try:
        return messages.get(TaskStatus(status), "未知状态")
    except ValueError:
        return "未知状态"
