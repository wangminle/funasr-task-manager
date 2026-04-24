"""Unit tests for the funasr-task-manager-reset-test-db skill script."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "6-skills" / "funasr-task-manager-reset-test-db" / "scripts" / "reset_db.py"

SERVER_TABLE_SQL = """
CREATE TABLE server_instances (
    server_id TEXT PRIMARY KEY,
    name TEXT,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    protocol_version TEXT NOT NULL,
    server_type TEXT,
    supported_modes TEXT,
    max_concurrency INTEGER NOT NULL DEFAULT 4,
    rtf_baseline REAL,
    penalty_factor REAL DEFAULT 0.1,
    status TEXT NOT NULL DEFAULT 'ONLINE',
    last_heartbeat TEXT,
    labels_json TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""

FILE_TABLE_SQL = """
CREATE TABLE files (
    file_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    media_type TEXT,
    mime TEXT,
    duration_sec REAL,
    codec TEXT,
    sample_rate INTEGER,
    channels INTEGER,
    size_bytes INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    checksum_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'UPLOADED',
    created_at TEXT,
    updated_at TEXT
)
"""

TASK_TABLE_SQL = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    task_group_id TEXT,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    eta_seconds INTEGER,
    assigned_server_id TEXT,
    external_vendor TEXT,
    external_task_id TEXT,
    language TEXT,
    options_json TEXT,
    result_path TEXT,
    error_code TEXT,
    error_message TEXT,
    retry_count INTEGER,
    callback_url TEXT,
    callback_secret TEXT,
    created_at TEXT,
    started_at TEXT,
    completed_at TEXT
)
"""


class ScriptFinished(Exception):
    def __init__(self, success: bool, message: str, data: dict):
        super().__init__(message)
        self.success = success
        self.message = message
        self.data = data


def _load_script_module():
    spec = importlib.util.spec_from_file_location("reset_asr_db_before_test_main", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _prepare_backend(tmp_path: Path, create_db: bool = False, with_server: bool = False) -> tuple[Path, Path]:
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir(parents=True, exist_ok=True)
    data_dir = tmp_path / "runtime" / "storage"
    data_dir.mkdir(parents=True)
    (backend_dir / "alembic.ini").write_text("sqlalchemy.url = sqlite+aiosqlite:////app/runtime/storage/asr_tasks.db\n", encoding="utf-8")

    db_path = data_dir / "asr_tasks.db"
    if create_db:
        conn = sqlite3.connect(db_path)
        conn.execute(SERVER_TABLE_SQL)
        if with_server:
            conn.execute(
                """
                INSERT INTO server_instances (
                    server_id, name, host, port, protocol_version, server_type,
                    supported_modes, max_concurrency, rtf_baseline, penalty_factor,
                    status, last_heartbeat, labels_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "existing-01",
                    "Existing Node",
                    "127.0.0.1",
                    10095,
                    "v2_new",
                    None,
                    None,
                    4,
                    0.2,
                    0.1,
                    "ONLINE",
                    None,
                    None,
                    "2026-04-01T00:00:00Z",
                    "2026-04-01T00:00:00Z",
                ),
            )
        conn.commit()
        conn.close()

    return backend_dir, db_path


def _seed_operational_data(backend_dir: Path, db_path: Path):
    storage_dir = db_path.parent
    results_dir = storage_dir / "results"
    temp_dir = storage_dir / "temp"
    uploads_dir = storage_dir / "uploads"
    results_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    (results_dir / "task-1.txt").write_bytes(b"hello-result")
    (results_dir / "task-2.json").write_bytes(b'{"text": "ok"}')
    (temp_dir / "chunk.tmp").write_bytes(b"temp-cache")
    (uploads_dir / "input.wav").write_bytes(b"upload-binary")

    conn = sqlite3.connect(db_path)
    conn.execute(FILE_TABLE_SQL)
    conn.execute(TASK_TABLE_SQL)
    conn.execute(
        """
        INSERT INTO files (file_id, user_id, original_name, size_bytes, storage_path, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("F1", "U1", "demo.wav", 12345, "runtime/storage/uploads/input.wav", "UPLOADED", "2026-04-01T08:00:00Z", "2026-04-01T08:00:00Z"),
    )
    conn.executemany(
        """
        INSERT INTO tasks (
            task_id, user_id, file_id, task_group_id, status, progress, assigned_server_id,
            language, result_path, retry_count, created_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("T1", "U1", "F1", "G1", "SUCCEEDED", 1.0, "existing-01", "zh", "runtime/storage/results/task-1.txt", 0, "2026-04-01T08:10:00Z", "2026-04-01T08:12:00Z"),
            ("T2", "U1", "F1", "G1", "QUEUED", 0.2, "existing-01", "zh", None, 0, "2026-04-01T08:20:00Z", None),
        ],
    )
    conn.commit()
    conn.close()


def _fake_alembic_upgrade(db_path: Path):
    def _run(*args, **kwargs):
        conn = sqlite3.connect(db_path)
        conn.execute(SERVER_TABLE_SQL)
        conn.commit()
        conn.close()
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    return _run


def _execute_main(module, monkeypatch: pytest.MonkeyPatch, argv: list[str]):
    def _finish(success: bool, message: str, data: dict | None = None):
        raise ScriptFinished(success, message, data or {})

    monkeypatch.setattr(module, "output_result", _finish)
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(ScriptFinished) as result:
        module.main()
    return result.value


@pytest.mark.unit
def test_recreates_missing_database_without_existing_db(tmp_path, monkeypatch):
    module = _load_script_module()
    backend_dir, db_path = _prepare_backend(tmp_path, create_db=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "BACKEND_DIR", str(backend_dir))
    monkeypatch.setattr(module, "BACKUP_DIR", str(tmp_path / "runtime" / "storage" / "backups"))
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "check_database_in_use", lambda _db_path: None)
    monkeypatch.setattr(module, "run_alembic_upgrade", _fake_alembic_upgrade(db_path))

    result = _execute_main(module, monkeypatch, ["main.py"])

    assert result.success is True
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    row_count = conn.execute("SELECT COUNT(*) FROM server_instances").fetchone()[0]
    conn.close()
    assert row_count == 0
    assert "backup_path" not in result.data


@pytest.mark.unit
def test_preserves_existing_servers_when_not_resetting(tmp_path, monkeypatch):
    module = _load_script_module()
    backend_dir, db_path = _prepare_backend(tmp_path, create_db=True, with_server=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "BACKEND_DIR", str(backend_dir))
    monkeypatch.setattr(module, "BACKUP_DIR", str(tmp_path / "runtime" / "storage" / "backups"))
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "check_database_in_use", lambda _db_path: None)
    monkeypatch.setattr(module, "run_alembic_upgrade", _fake_alembic_upgrade(db_path))

    result = _execute_main(module, monkeypatch, ["main.py"])

    assert result.success is True
    assert Path(result.data["backup_path"]).exists()
    conn = sqlite3.connect(db_path)
    restored = conn.execute(
        "SELECT server_id, name, host, port, protocol_version, status FROM server_instances"
    ).fetchall()
    conn.close()
    assert restored == [("existing-01", "Existing Node", "127.0.0.1", 10095, "v2_new", "ONLINE")]
    assert result.data["servers_preserved"] == 1


@pytest.mark.unit
def test_seeds_default_servers_when_reset_servers_enabled(tmp_path, monkeypatch):
    module = _load_script_module()
    backend_dir, db_path = _prepare_backend(tmp_path, create_db=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "BACKEND_DIR", str(backend_dir))
    monkeypatch.setattr(module, "BACKUP_DIR", str(tmp_path / "runtime" / "storage" / "backups"))
    monkeypatch.setattr(module, "DB_PATH", str(db_path))
    monkeypatch.setattr(module, "check_database_in_use", lambda _db_path: None)
    monkeypatch.setattr(module, "run_alembic_upgrade", _fake_alembic_upgrade(db_path))

    result = _execute_main(module, monkeypatch, ["main.py", "--reset-servers"])

    assert result.success is True
    conn = sqlite3.connect(db_path)
    seeded = conn.execute(
        "SELECT server_id, host, port FROM server_instances ORDER BY server_id"
    ).fetchall()
    conn.close()
    assert seeded == [
        ("asr-local-01", "127.0.0.1", 10095),
        ("asr-local-02", "127.0.0.1", 10096),
        ("asr-local-03", "127.0.0.1", 10097),
    ]
    assert result.data["seed_data_inserted"] is True


@pytest.mark.unit
def test_dry_run_reports_storage_servers_and_task_summary(tmp_path, monkeypatch):
    module = _load_script_module()
    backend_dir, db_path = _prepare_backend(tmp_path, create_db=True, with_server=True)
    _seed_operational_data(backend_dir, db_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module, "BACKEND_DIR", str(backend_dir))
    monkeypatch.setattr(module, "BACKUP_DIR", str(backend_dir / "data" / "backups"))
    monkeypatch.setattr(module, "DB_PATH", str(db_path))

    def _unexpected_run(*args, **kwargs):
        raise AssertionError("dry-run should not invoke migrations")

    monkeypatch.setattr(module, "run_alembic_upgrade", _unexpected_run)

    result = _execute_main(module, monkeypatch, ["main.py", "--dry-run"])

    assert result.success is True
    assert result.data["dry_run"] is True
    assert result.data["servers"]["count"] == 1
    assert result.data["database_files"]["count"] == 1
    assert result.data["tasks"]["total"] == 2
    assert result.data["tasks"]["by_status"] == {"QUEUED": 1, "SUCCEEDED": 1}
    assert result.data["results"]["file_count"] == 2
    assert result.data["estimated_savings"]["database_only_bytes"] > 0
    assert result.data["estimated_savings"]["full_reset_bytes"] > result.data["estimated_savings"]["database_only_bytes"]
    assert "预计可释放" in result.data["summary"]
