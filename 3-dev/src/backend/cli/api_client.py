"""HTTP API client wrapping httpx for all backend calls."""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import httpx

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class ASRClient:
    """Thin HTTP client for the ASR Task Manager API."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0):
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)
        self._last_status: int | None = None

    def close(self) -> None:
        self._client.close()

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise APIError(resp.status_code, str(detail))
        return resp

    @property
    def last_status_code(self) -> int | None:
        """Status code of the most recent response (for 2xx variant checks)."""
        return self._last_status

    def _check_with_status(self, resp: httpx.Response) -> httpx.Response:
        """Like _check, but also stores the HTTP status code for 2xx inspection."""
        self._last_status = resp.status_code
        return self._check(resp)

    def _stream_ndjson(self, method: str, url: str, **kwargs) -> Iterator[dict]:
        """Iterate NDJSON lines from a streaming HTTP response."""
        with self._client.stream(method, url, **kwargs) as resp:
            if resp.status_code >= 400:
                resp.read()
                self._check(resp)
            for line in resp.iter_lines():
                line = line.strip()
                if line:
                    try:
                        yield _json.loads(line)
                    except _json.JSONDecodeError:
                        logger.warning("收到无法解析的进度事件, raw_line=%s", line)

    # --- health ---
    def health(self) -> dict:
        return self._check(self._client.get("/health")).json()

    def metrics(self) -> str:
        return self._check(self._client.get("/metrics")).text

    def stats(self, global_stats: bool = False) -> dict:
        params = {}
        if global_stats:
            params["global"] = "true"
        return self._check(self._client.get("/api/v1/stats", params=params)).json()

    # --- files ---
    def upload_file(self, path: Path) -> dict:
        with open(path, "rb") as f:
            resp = self._client.post(
                "/api/v1/files/upload",
                files={"file": (path.name, f)},
            )
        return self._check(resp).json()

    def file_info(self, file_id: str) -> dict:
        return self._check(self._client.get(f"/api/v1/files/{file_id}")).json()

    # --- tasks ---
    def create_tasks(
        self, items: list[dict], callback: dict | None = None,
        segment_level: str = "10m",
    ) -> list[dict]:
        body: dict[str, Any] = {"items": items}
        if callback:
            body["callback"] = callback
        body["segment_level"] = segment_level
        return self._check(self._client.post("/api/v1/tasks", json=body)).json()

    def list_tasks(
        self, status: str | None = None, search: str | None = None,
        group: str | None = None,
        page: int = 1, page_size: int = 20,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        if search:
            params["search"] = search
        if group:
            params["group"] = group
        return self._check(self._client.get("/api/v1/tasks", params=params)).json()

    def get_task(self, task_id: str) -> dict:
        return self._check(self._client.get(f"/api/v1/tasks/{task_id}")).json()

    def cancel_task(self, task_id: str) -> dict:
        return self._check(self._client.post(f"/api/v1/tasks/{task_id}/cancel")).json()

    def get_result(self, task_id: str, fmt: str = "json") -> str:
        resp = self._check(self._client.get(f"/api/v1/tasks/{task_id}/result", params={"format": fmt}))
        return resp.text

    def task_progress_stream(self, task_id: str):
        """Yield SSE events as dicts from the progress endpoint."""
        with self._client.stream("GET", f"/api/v1/tasks/{task_id}/progress") as resp:
            if resp.status_code >= 400:
                raise APIError(resp.status_code, "SSE connection failed")
            event_data = ""
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    event_data = line[5:].strip()
                elif line == "" and event_data:
                    try:
                        yield _json.loads(event_data)
                    except _json.JSONDecodeError:
                        logger.warning("收到无法解析的SSE事件, raw_data=%s", event_data)
                    event_data = ""
            # Flush trailing event_data if connection closed without a final empty line
            if event_data:
                try:
                    yield _json.loads(event_data)
                except _json.JSONDecodeError:
                    logger.warning("收到无法解析的SSE事件(尾帧), raw_data=%s", event_data)

    # --- task groups ---
    def get_task_group(self, group_id: str) -> dict:
        return self._check(self._client.get(f"/api/v1/task-groups/{group_id}")).json()

    def list_group_tasks(self, group_id: str, page: int = 1, page_size: int = 100) -> dict:
        params = {"page": page, "page_size": page_size}
        return self._check(self._client.get(f"/api/v1/task-groups/{group_id}/tasks", params=params)).json()

    def get_group_results(self, group_id: str, fmt: str = "txt") -> str:
        resp = self._check(self._client.get(f"/api/v1/task-groups/{group_id}/results", params={"format": fmt}))
        return resp.text

    def delete_task_group(self, group_id: str) -> dict:
        return self._check(self._client.delete(f"/api/v1/task-groups/{group_id}")).json()

    # --- diagnostics ---
    def diagnostics(self) -> dict:
        return self._check(self._client.get("/api/v1/diagnostics")).json()

    # --- servers ---
    def list_servers(self) -> list[dict]:
        return self._check(self._client.get("/api/v1/servers")).json()

    def register_server(self, data: dict) -> dict:
        if not data.get("run_benchmark"):
            return self._check(self._client.post(
                "/api/v1/servers", json=data, timeout=30.0,
            )).json()
        result: dict = {}
        for event in self.register_server_stream(data):
            if event.get("type") == "server_registered":
                result = event.get("data", {})
        return result

    def register_server_stream(self, data: dict) -> Iterator[dict]:
        """Yield NDJSON progress events for register + benchmark."""
        yield from self._stream_ndjson(
            "POST", "/api/v1/servers", json=data, timeout=960.0,
        )

    def probe_server(self, server_id: str, level: str = "offline_light") -> dict:
        return self._check(self._client.post(
            f"/api/v1/servers/{server_id}/probe", params={"level": level}
        )).json()

    def benchmark_server(self, server_id: str, timeout: float = 960.0) -> dict:
        """Non-streaming wrapper: collects final benchmark_result."""
        for event in self.benchmark_server_stream(server_id, timeout=timeout):
            if event.get("type") == "benchmark_result":
                return event.get("data", {})
            if event.get("type") == "benchmark_error":
                raise APIError(422, event.get("error", "benchmark failed"))
        return {}

    def benchmark_server_stream(self, server_id: str, timeout: float = 960.0) -> Iterator[dict]:
        """Yield NDJSON progress events for a single server benchmark."""
        yield from self._stream_ndjson(
            "POST", f"/api/v1/servers/{server_id}/benchmark", timeout=timeout,
        )

    def benchmark_servers(self, timeout: float = 960.0) -> dict:
        """Non-streaming wrapper: collects final all_complete result."""
        for event in self.benchmark_servers_stream(timeout=timeout):
            if event.get("type") == "all_complete":
                return event.get("data", {})
        return {}

    def benchmark_servers_stream(self, timeout: float = 960.0) -> Iterator[dict]:
        """Yield NDJSON progress events for all-server benchmark."""
        yield from self._stream_ndjson(
            "POST", "/api/v1/servers/benchmark", timeout=timeout,
        )

    def update_server(self, server_id: str, data: dict) -> dict:
        return self._check(self._client.patch(f"/api/v1/servers/{server_id}", json=data)).json()

    def delete_server(self, server_id: str) -> None:
        self._check(self._client.delete(f"/api/v1/servers/{server_id}"))
