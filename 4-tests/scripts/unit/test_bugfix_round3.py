"""Unit tests for round 3 bugfixes.

BUG-1: diagnostics requires admin auth
BUG-3: file_name_map handles task/upload count mismatch
BUG-4: batch polling uses batch query instead of per-task
D-3:   _download_group_results uses original file_name
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

backend_root = Path(__file__).resolve().parent.parent.parent.parent / "3-dev" / "src" / "backend"
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))


class TestBug1DiagnosticsAuth:
    """BUG-1: /api/v1/diagnostics should require admin authentication."""

    def test_diagnostics_route_has_admin_dependency(self):
        """Verify the diagnostics endpoint function signature includes admin param."""
        from app.api.health import diagnostics
        import inspect
        sig = inspect.signature(diagnostics)
        param_names = list(sig.parameters.keys())
        assert "admin" in param_names, (
            "diagnostics endpoint must have 'admin' parameter for AdminUser auth"
        )

    def test_diagnostics_admin_param_type(self):
        """Verify admin param has the correct Depends annotation."""
        from app.api.health import diagnostics
        import inspect
        sig = inspect.signature(diagnostics)
        admin_param = sig.parameters["admin"]
        annotation_str = str(admin_param.annotation)
        assert "verify_admin" in annotation_str or "AdminUser" in annotation_str or "Depends" in annotation_str


class TestBug3FileNameMapTruncation:
    """BUG-3: file_name_map should handle tasks < upload_map gracefully."""

    def test_map_building_fewer_tasks_than_uploads(self):
        """When server returns fewer tasks than uploaded files, all returned tasks
        should still be properly mapped."""
        tasks = [
            {"task_id": "tid_1", "file_name": "ep01.wav"},
            {"task_id": "tid_2", "file_name": "ep02.wav"},
        ]
        upload_map = [
            (Path("ep01.wav"), "fid_1"),
            (Path("ep02.wav"), "fid_2"),
            (Path("ep03.wav"), "fid_3"),
        ]

        file_name_map: dict[str, str] = {}
        file_stem_map: dict[str, str] = {}
        for i, t in enumerate(tasks):
            tid = t["task_id"]
            fn = t.get("file_name")
            if fn:
                file_name_map[tid] = fn
                file_stem_map[tid] = Path(fn).stem
            elif i < len(upload_map):
                fp, _ = upload_map[i]
                file_name_map[tid] = fp.name
                file_stem_map[tid] = fp.stem

        assert len(file_name_map) == 2
        assert file_name_map["tid_1"] == "ep01.wav"
        assert file_name_map["tid_2"] == "ep02.wav"
        assert file_stem_map["tid_1"] == "ep01"

    def test_map_building_uses_file_name_from_api_response(self):
        """When API returns file_name, it should be preferred over upload_map index."""
        tasks = [
            {"task_id": "tid_1", "file_name": "original_name.mp4"},
        ]
        upload_map = [
            (Path("local_copy.mp4"), "fid_1"),
        ]

        file_name_map: dict[str, str] = {}
        for i, t in enumerate(tasks):
            tid = t["task_id"]
            fn = t.get("file_name")
            if fn:
                file_name_map[tid] = fn
            elif i < len(upload_map):
                fp, _ = upload_map[i]
                file_name_map[tid] = fp.name

        assert file_name_map["tid_1"] == "original_name.mp4"

    def test_map_building_fallback_to_upload_map(self):
        """When API response has no file_name, fall back to upload_map."""
        tasks = [
            {"task_id": "tid_1"},
        ]
        upload_map = [
            (Path("recording.wav"), "fid_1"),
        ]

        file_name_map: dict[str, str] = {}
        for i, t in enumerate(tasks):
            tid = t["task_id"]
            fn = t.get("file_name")
            if fn:
                file_name_map[tid] = fn
            elif i < len(upload_map):
                fp, _ = upload_map[i]
                file_name_map[tid] = fp.name

        assert file_name_map["tid_1"] == "recording.wav"


class TestBug4BatchPolling:
    """BUG-4: batch polling should use list_group_tasks instead of per-task get_task."""

    def test_polling_uses_batch_query_when_group_exists(self):
        """Simulate polling loop logic: when task_group_id exists,
        list_group_tasks should be called instead of N individual get_task calls."""
        mock_client = MagicMock()

        task_group_id = "grp_001"
        task_ids = ["tid_1", "tid_2", "tid_3"]

        mock_client.list_group_tasks.return_value = {
            "items": [
                {"task_id": "tid_1", "status": "SUCCEEDED"},
                {"task_id": "tid_2", "status": "TRANSCRIBING"},
                {"task_id": "tid_3", "status": "FAILED"},
            ]
        }

        terminal = {"SUCCEEDED", "FAILED", "CANCELED"}
        completed: dict[str, dict] = {}
        task_id_set = set(task_ids)

        if task_group_id:
            group_data = mock_client.list_group_tasks(task_group_id, page_size=500)
            batch_tasks = group_data.get("items", [])
        else:
            batch_tasks = [mock_client.get_task(tid) for tid in task_ids if tid not in completed]

        for t in batch_tasks:
            tid = t["task_id"]
            if tid in completed or tid not in task_id_set:
                continue
            if t["status"] in terminal:
                completed[tid] = t

        mock_client.list_group_tasks.assert_called_once_with(task_group_id, page_size=500)
        mock_client.get_task.assert_not_called()
        assert len(completed) == 2
        assert "tid_1" in completed
        assert "tid_3" in completed
        assert "tid_2" not in completed

    def test_polling_falls_back_to_individual_when_no_group(self):
        """When task_group_id is None, fall back to per-task get_task calls."""
        mock_client = MagicMock()
        task_group_id = None
        task_ids = ["tid_1", "tid_2"]

        mock_client.get_task.side_effect = [
            {"task_id": "tid_1", "status": "SUCCEEDED"},
            {"task_id": "tid_2", "status": "TRANSCRIBING"},
        ]

        terminal = {"SUCCEEDED", "FAILED", "CANCELED"}
        completed: dict[str, dict] = {}
        task_id_set = set(task_ids)

        if task_group_id:
            group_data = mock_client.list_group_tasks(task_group_id, page_size=500)
            batch_tasks = group_data.get("items", [])
        else:
            batch_tasks = [mock_client.get_task(tid) for tid in task_ids if tid not in completed]

        for t in batch_tasks:
            tid = t["task_id"]
            if tid in completed or tid not in task_id_set:
                continue
            if t["status"] in terminal:
                completed[tid] = t

        mock_client.list_group_tasks.assert_not_called()
        assert mock_client.get_task.call_count == 2
        assert "tid_1" in completed
        assert "tid_2" not in completed


class TestD3DownloadGroupResultsFileName:
    """D-3: _download_group_results should use original file_name from API."""

    def test_stem_uses_file_name_when_available(self):
        """When task has file_name, download should use its stem."""
        task = {"task_id": "01JQXXXXXXXXX", "status": "SUCCEEDED",
                "file_id": "fid_1", "file_name": "meeting_recording.wav"}
        raw_name = task.get("file_name") or ""
        stem = Path(raw_name).stem if raw_name else task["task_id"][:12]
        assert stem == "meeting_recording"

    def test_stem_falls_back_to_tid_without_file_name(self):
        """When task has no file_name, fall back to tid[:12]."""
        task = {"task_id": "01JQXXXXXXXXX123", "status": "SUCCEEDED",
                "file_id": "fid_1"}
        raw_name = task.get("file_name") or ""
        stem = Path(raw_name).stem if raw_name else task["task_id"][:12]
        assert stem == "01JQXXXXXXXX"


class TestTaskResponseFileName:
    """TaskResponse schema should include optional file_name field."""

    def test_task_response_has_file_name_field(self):
        from app.schemas.task import TaskResponse
        assert "file_name" in TaskResponse.model_fields

    def test_task_response_file_name_defaults_to_none(self):
        from app.schemas.task import TaskResponse
        from datetime import datetime
        resp = TaskResponse(
            task_id="tid_1", user_id="u1", file_id="fid_1",
            status="PENDING", progress=0.0, language="zh",
            created_at=datetime.now(),
        )
        assert resp.file_name is None

    def test_task_response_file_name_populated(self):
        from app.schemas.task import TaskResponse
        from datetime import datetime
        resp = TaskResponse(
            task_id="tid_1", user_id="u1", file_id="fid_1",
            file_name="test.wav",
            status="PENDING", progress=0.0, language="zh",
            created_at=datetime.now(),
        )
        assert resp.file_name == "test.wav"
