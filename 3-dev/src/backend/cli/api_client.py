"""HTTP API client wrapping httpx for all backend calls."""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO

import httpx


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

    # --- health ---
    def health(self) -> dict:
        return self._check(self._client.get("/health")).json()

    def metrics(self) -> str:
        return self._check(self._client.get("/metrics")).text

    def stats(self) -> dict:
        return self._check(self._client.get("/api/v1/stats")).json()

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
    def create_tasks(self, items: list[dict], callback: dict | None = None) -> list[dict]:
        body: dict[str, Any] = {"items": items}
        if callback:
            body["callback"] = callback
        return self._check(self._client.post("/api/v1/tasks", json=body)).json()

    def list_tasks(
        self, status: str | None = None, search: str | None = None,
        page: int = 1, page_size: int = 20,
    ) -> dict:
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        if search:
            params["search"] = search
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
                    import json
                    try:
                        yield json.loads(event_data)
                    except json.JSONDecodeError:
                        pass
                    event_data = ""

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
        return self._check(self._client.post("/api/v1/servers", json=data)).json()

    def probe_server(self, server_id: str, level: str = "offline_light") -> dict:
        return self._check(self._client.post(
            f"/api/v1/servers/{server_id}/probe", params={"level": level}
        )).json()

    def benchmark_servers(self) -> dict:
        return self._check(self._client.post("/api/v1/servers/benchmark")).json()

    def update_server(self, server_id: str, data: dict) -> dict:
        return self._check(self._client.patch(f"/api/v1/servers/{server_id}", json=data)).json()

    def delete_server(self, server_id: str) -> None:
        self._check(self._client.delete(f"/api/v1/servers/{server_id}"))
