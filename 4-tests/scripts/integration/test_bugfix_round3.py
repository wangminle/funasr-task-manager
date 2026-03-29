"""Integration tests for round 3 bugfixes.

BUG-1: diagnostics requires admin auth
BUG-2: DELETE /task-groups returns 207 on partial deletion
D-1:   _group_stats() optimized query still returns correct data
D-3:   TaskResponse includes file_name from File relationship
"""

import io

import pytest
from sqlalchemy import update


async def _upload_file(client, name: str = "test.wav") -> str:
    content = b"RIFF" + b"\x00" * 500
    files = {"file": (name, io.BytesIO(content), "audio/wav")}
    resp = await client.post("/api/v1/files/upload", files=files)
    assert resp.status_code == 201
    return resp.json()["file_id"]


async def _create_batch(client, count: int = 3, names: list[str] | None = None) -> tuple[str, list[str]]:
    if names is None:
        names = [f"test_{i}.wav" for i in range(count)]
    file_ids = [await _upload_file(client, name=n) for n in names]
    body = {"items": [{"file_id": fid, "language": "zh"} for fid in file_ids]}
    resp = await client.post("/api/v1/tasks", json=body)
    assert resp.status_code == 201
    tasks = resp.json()
    group_id = tasks[0]["task_group_id"]
    task_ids = [t["task_id"] for t in tasks]
    return group_id, task_ids


@pytest.mark.integration
class TestBug1DiagnosticsAuthIntegration:
    """BUG-1: diagnostics should be accessible (auth disabled in test env)."""

    async def test_diagnostics_still_accessible_when_auth_disabled(self, client):
        resp = await client.get("/api/v1/diagnostics")
        assert resp.status_code == 200
        data = resp.json()
        assert "checks" in data

    async def test_diagnostics_requires_admin_when_auth_enabled(self, client):
        """When auth is enabled, non-admin or missing token should fail."""
        from app.auth.token import configure_auth
        original_tokens = {"dev-token-user1": "user1", "dev-token-admin": "admin"}
        configure_auth(token_map=original_tokens, enabled=True)
        try:
            resp_no_token = await client.get("/api/v1/diagnostics")
            assert resp_no_token.status_code == 401

            resp_user = await client.get(
                "/api/v1/diagnostics",
                headers={"X-API-Key": "dev-token-user1"},
            )
            assert resp_user.status_code == 403

            resp_admin = await client.get(
                "/api/v1/diagnostics",
                headers={"X-API-Key": "dev-token-admin"},
            )
            assert resp_admin.status_code == 200
        finally:
            configure_auth(enabled=False)


@pytest.mark.integration
class TestBug2DeletePartialStatus:
    """BUG-2: DELETE should return 207 when active tasks are skipped."""

    async def test_delete_returns_200_when_all_deleted(self, client):
        group_id, _ = await _create_batch(client, 2)
        resp = await client.delete(f"/api/v1/task-groups/{group_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 2
        assert data["skipped_active"] == 0
        assert data["partial"] is False

    async def test_delete_returns_207_when_active_tasks_skipped(self, client, db_session):
        group_id, task_ids = await _create_batch(client, 3)

        from app.models import Task
        await db_session.execute(
            update(Task).where(Task.task_id == task_ids[0]).values(status="DISPATCHED")
        )
        await db_session.commit()

        resp = await client.delete(f"/api/v1/task-groups/{group_id}")
        assert resp.status_code == 207
        data = resp.json()
        assert data["skipped_active"] == 1
        assert data["deleted"] == 2
        assert data["partial"] is True


@pytest.mark.integration
class TestD1OptimizedGroupStats:
    """D-1: _group_stats() with optimized query should return correct results."""

    async def test_group_stats_correct_counts(self, client, db_session):
        group_id, task_ids = await _create_batch(client, 4)

        from app.models import Task
        await db_session.execute(
            update(Task).where(Task.task_id == task_ids[0]).values(status="SUCCEEDED", progress=1.0)
        )
        await db_session.execute(
            update(Task).where(Task.task_id == task_ids[1]).values(status="FAILED", progress=0.5)
        )
        await db_session.execute(
            update(Task).where(Task.task_id == task_ids[2]).values(status="CANCELED", progress=0.0)
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/task-groups/{group_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        assert data["succeeded"] == 1
        assert data["failed"] == 1
        assert data["canceled"] == 1
        assert data["in_progress"] == 1
        assert data["is_complete"] is False

    async def test_group_stats_all_complete(self, client, db_session):
        group_id, task_ids = await _create_batch(client, 2)

        from app.models import Task
        for tid in task_ids:
            await db_session.execute(
                update(Task).where(Task.task_id == tid).values(status="SUCCEEDED", progress=1.0)
            )
        await db_session.commit()

        resp = await client.get(f"/api/v1/task-groups/{group_id}")
        data = resp.json()
        assert data["is_complete"] is True
        assert data["progress"] == 1.0


@pytest.mark.integration
class TestD3FileNameInTaskResponse:
    """D-3: TaskResponse should include file_name from File relationship."""

    async def test_task_response_includes_file_name(self, client):
        fid = await _upload_file(client, name="my_audio_recording.wav")
        resp = await client.post("/api/v1/tasks", json={
            "items": [{"file_id": fid, "language": "zh"}]
        })
        assert resp.status_code == 201
        task = resp.json()[0]
        assert task.get("file_name") == "my_audio_recording.wav"

    async def test_task_list_includes_file_name(self, client):
        group_id, _ = await _create_batch(client, 2, names=["ep01.wav", "ep02.wav"])
        resp = await client.get(f"/api/v1/task-groups/{group_id}/tasks")
        data = resp.json()
        file_names = sorted([t["file_name"] for t in data["items"]])
        assert file_names == ["ep01.wav", "ep02.wav"]

    async def test_get_single_task_includes_file_name(self, client):
        fid = await _upload_file(client, name="speech.mp4")
        resp = await client.post("/api/v1/tasks", json={
            "items": [{"file_id": fid}]
        })
        task_id = resp.json()[0]["task_id"]

        resp2 = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp2.status_code == 200
        assert resp2.json()["file_name"] == "speech.mp4"
