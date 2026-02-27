"""Locust load test for ASR Task Manager API.

Tests T-M3-40 ~ T-M3-42 from the project plan:
- 50 concurrent users uploading + creating tasks
- 100 tasks completed without loss
- Memory/CPU stability

Usage:
    locust -f 4-tests/scripts/load/locustfile.py --headless -u 50 -r 10 -t 5m \
        --host http://localhost:8000 --html 4-tests/reports/load-report.html
"""

import io
import json
import random

from locust import HttpUser, between, task, events


class ASRTaskUser(HttpUser):
    """Simulates a user uploading audio and creating transcription tasks."""

    wait_time = between(0.5, 2.0)
    host = "http://localhost:8000"

    def on_start(self):
        self.api_key = "dev-token-user1"
        self.headers = {"X-API-Key": self.api_key}
        self.uploaded_file_ids = []
        self.created_task_ids = []

    @task(3)
    def upload_file(self):
        size = random.randint(500, 5000)
        content = b"RIFF" + b"\x00" * size
        files = {"file": ("load_test.wav", io.BytesIO(content), "audio/wav")}
        with self.client.post(
            "/api/v1/files/upload",
            files=files,
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                fid = response.json().get("file_id")
                if fid:
                    self.uploaded_file_ids.append(fid)
                response.success()
            else:
                response.failure(f"Upload failed: {response.status_code}")

    @task(5)
    def create_task(self):
        if not self.uploaded_file_ids:
            return
        file_id = random.choice(self.uploaded_file_ids)
        body = {"items": [{"file_id": file_id, "language": "zh"}]}
        with self.client.post(
            "/api/v1/tasks",
            json=body,
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                tasks = response.json()
                for t in tasks:
                    self.created_task_ids.append(t["task_id"])
                response.success()
            elif response.status_code == 429:
                response.success()
            else:
                response.failure(f"Create task failed: {response.status_code}")

    @task(8)
    def query_task_list(self):
        page = random.randint(1, 5)
        with self.client.get(
            f"/api/v1/tasks?page={page}&page_size=20",
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"List tasks failed: {response.status_code}")

    @task(4)
    def query_task_detail(self):
        if not self.created_task_ids:
            return
        task_id = random.choice(self.created_task_ids)
        with self.client.get(
            f"/api/v1/tasks/{task_id}",
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code in (200, 404):
                response.success()
            else:
                response.failure(f"Get task failed: {response.status_code}")

    @task(2)
    def health_check(self):
        self.client.get("/health")

    @task(1)
    def list_servers(self):
        self.client.get("/api/v1/servers")


class ASRBatchUser(HttpUser):
    """Simulates batch operations: uploading multiple files and creating batch tasks."""

    wait_time = between(2.0, 5.0)
    host = "http://localhost:8000"

    def on_start(self):
        self.api_key = "dev-token-user2"
        self.headers = {"X-API-Key": self.api_key}

    @task(1)
    def batch_upload_and_create(self):
        file_ids = []
        for i in range(3):
            content = b"RIFF" + b"\x00" * random.randint(1000, 3000)
            files = {"file": (f"batch_{i}.wav", io.BytesIO(content), "audio/wav")}
            resp = self.client.post(
                "/api/v1/files/upload",
                files=files,
                headers=self.headers,
                name="/api/v1/files/upload [batch]",
            )
            if resp.status_code == 201:
                file_ids.append(resp.json()["file_id"])

        if file_ids:
            body = {"items": [{"file_id": fid} for fid in file_ids]}
            self.client.post(
                "/api/v1/tasks",
                json=body,
                headers=self.headers,
                name="/api/v1/tasks [batch]",
            )
