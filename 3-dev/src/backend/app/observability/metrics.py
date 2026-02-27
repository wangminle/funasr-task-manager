"""Prometheus metrics definitions for the ASR Task Manager."""

from prometheus_client import Counter, Gauge, Histogram

asr_tasks_total = Counter("asr_tasks_total", "Total number of ASR tasks by status", ["status"])
asr_task_duration_seconds = Histogram("asr_task_duration_seconds", "Time from task creation to completion", buckets=[10, 30, 60, 120, 300, 600, 1800, 3600])
asr_queue_depth = Gauge("asr_queue_depth", "Current number of tasks waiting in queue")
asr_server_active_tasks = Gauge("asr_server_active_tasks", "Number of currently active tasks per server", ["server_id"])
asr_server_rtf = Histogram("asr_server_rtf", "Real-Time Factor per server", ["server_id"], buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0])
asr_server_last_heartbeat = Gauge("asr_server_last_heartbeat", "Unix timestamp of last heartbeat per server", ["server_id"])
asr_api_requests_total = Counter("asr_api_requests_total", "Total API requests by method and path", ["method", "path", "status_code"])
asr_upload_bytes_total = Counter("asr_upload_bytes_total", "Total bytes uploaded")

asr_task_retries_total = Counter("asr_task_retries_total", "Total number of task retries")
asr_circuit_breaker_state = Gauge("asr_circuit_breaker_state", "Circuit breaker state per server (0=CLOSED,1=OPEN,2=HALF_OPEN)", ["server_id"])
asr_server_slots_used = Gauge("asr_server_slots_used", "Used concurrency slots per server", ["server_id"])
asr_callback_deliveries_total = Counter("asr_callback_deliveries_total", "Callback delivery attempts", ["status"])
asr_rate_limit_rejections_total = Counter("asr_rate_limit_rejections_total", "Rate limit rejections", ["dimension"])
