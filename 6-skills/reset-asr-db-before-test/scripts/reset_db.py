#!/usr/bin/env python3
"""测试前数据库重置工具。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = BASE_DIR / "3-dev" / "src" / "backend"
BACKUP_DIR = BACKEND_DIR / "data" / "backups"
DB_PATH = BACKEND_DIR / "data" / "asr_tasks.db"
TEST_DATA_SQL = """
-- 本地测试ASR服务器（默认本地3节点配置）
INSERT INTO server_instances (server_id, host, port, protocol_version, max_concurrency, status, name, rtf_baseline) VALUES
('asr-local-01', '127.0.0.1', 10095, 'v2_new', 4, 'ONLINE', '本地测试节点1', 0.12),
('asr-local-02', '127.0.0.1', 10096, 'v2_new', 4, 'ONLINE', '本地测试节点2', 0.35),
('asr-local-03', '127.0.0.1', 10097, 'v2_new', 4, 'ONLINE', '本地测试节点3', 0.42);
"""


def output_result(success: bool, message: str, data: dict | None = None):
    """统一结构化输出，适合CI解析"""
    result = {
        "status": "success" if success else "failed",
        "message": message,
        "data": data or {}
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    raise SystemExit(0 if success else 1)


def _path(value: str | Path) -> Path:
    return Path(value)


def format_bytes(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024


def build_file_record(file_path: Path) -> dict:
    stat = file_path.stat()
    return {
        "path": str(file_path),
        "name": file_path.name,
        "size_bytes": stat.st_size,
        "size_human": format_bytes(stat.st_size),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def summarize_directory(path: str | Path, latest_limit: int = 5) -> dict:
    directory = _path(path)
    if not directory.exists():
        return {
            "path": str(directory),
            "exists": False,
            "file_count": 0,
            "total_size_bytes": 0,
            "total_size_human": format_bytes(0),
            "latest_files": [],
        }

    files = [file_path for file_path in directory.rglob("*") if file_path.is_file()]
    latest_files = sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)[:latest_limit]
    total_size = sum(file_path.stat().st_size for file_path in files)
    return {
        "path": str(directory),
        "exists": True,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_human": format_bytes(total_size),
        "latest_files": [build_file_record(file_path) for file_path in latest_files],
    }


def summarize_database_files(db_path: str | Path) -> dict:
    database_path = _path(db_path)
    candidates = [
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        Path(f"{database_path}-journal"),
    ]
    existing_files = [candidate for candidate in candidates if candidate.exists() and candidate.is_file()]
    total_size = sum(file_path.stat().st_size for file_path in existing_files)
    return {
        "path": str(database_path),
        "count": len(existing_files),
        "total_size_bytes": total_size,
        "total_size_human": format_bytes(total_size),
        "items": [build_file_record(file_path) for file_path in existing_files],
    }


def _check_file_handle_in_use(file_path: Path) -> bool:
    """Try to detect whether another process holds an open handle on *file_path*.

    On Windows ``os.rename(f, f)`` raises ``PermissionError`` when another
    process has the file open (even idle ``sqlite3.connect()``).  On POSIX
    the same call is a no-op, so we additionally try to open the file with
    an exclusive advisory lock via ``fcntl.flock``.
    """
    if not file_path.exists():
        return False

    if sys.platform == "win32":
        try:
            os.rename(str(file_path), str(file_path))
            return False
        except (PermissionError, OSError):
            return True
    else:
        try:
            import fcntl
            fd = os.open(str(file_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            except (OSError, BlockingIOError):
                return True
            finally:
                os.close(fd)
        except (ImportError, OSError):
            return False


def check_database_in_use(db_path: str | Path) -> str | None:
    """如果数据库正被其他进程占用，返回诊断信息；否则返回 None。"""
    target = _path(db_path)
    if not target.exists():
        return None

    if sys.platform != "win32":
        try:
            files = [target] + [
                Path(f"{target}{s}")
                for s in ("-wal", "-shm")
                if Path(f"{target}{s}").exists()
            ]
            proc = subprocess.run(
                ["lsof", "-t"] + [str(f) for f in files],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                pids = {p.strip() for p in proc.stdout.strip().splitlines()}
                pids.discard(str(os.getpid()))
                if pids:
                    return (
                        f"数据库文件正被其他进程占用 (PID: {', '.join(sorted(pids))})，"
                        "请先停止后端服务再执行重置"
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    db_files = [target] + [
        Path(f"{target}{s}")
        for s in ("-wal", "-shm", "-journal")
        if Path(f"{target}{s}").exists()
    ]
    for f in db_files:
        if _check_file_handle_in_use(f):
            return (
                f"数据库文件 {f.name} 正被其他进程占用（文件句柄未释放），"
                "请先停止后端服务再执行重置"
            )

    try:
        conn = sqlite3.connect(str(target), timeout=1)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.rollback()
        except sqlite3.OperationalError:
            return "数据库被锁定，请先停止占用数据库的进程"
        except sqlite3.DatabaseError:
            pass
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        pass

    return None


def database_exists(db_path: str | Path) -> bool:
    return _path(db_path).exists()


def table_exists(db_path: str | Path, table_name: str) -> bool:
    if not database_exists(db_path):
        return False

    conn = sqlite3.connect(_path(db_path))
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    )
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def get_table_columns(db_path: str | Path, table_name: str) -> list[str]:
    conn = sqlite3.connect(_path(db_path))
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    if rows:
        name_index = columns.index("name")
        return [row[name_index] for row in rows]
    return []


def export_servers(db_path: str | Path) -> list[dict]:
    """导出已有服务器配置。"""
    if not table_exists(db_path, "server_instances"):
        return []

    conn = sqlite3.connect(_path(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM server_instances")
    columns = [desc[0] for desc in cursor.description]
    servers = [dict(zip(columns, row)) for row in cursor.fetchall()]
    conn.close()
    return servers


def summarize_servers(db_path: str | Path) -> dict:
    servers = export_servers(db_path)
    status_counts: dict[str, int] = {}
    for server in servers:
        status = str(server.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1

    items = []
    for server in servers:
        items.append({
            "server_id": server.get("server_id"),
            "name": server.get("name"),
            "host": server.get("host"),
            "port": server.get("port"),
            "protocol_version": server.get("protocol_version"),
            "status": server.get("status"),
            "max_concurrency": server.get("max_concurrency"),
            "rtf_baseline": server.get("rtf_baseline"),
        })

    return {
        "count": len(servers),
        "by_status": dict(sorted(status_counts.items())),
        "items": items,
    }


def import_servers(db_path: str | Path, servers: list[dict]):
    """导入服务器配置到新数据库。"""
    if not servers:
        return

    available_columns = set(get_table_columns(db_path, "server_instances"))
    insert_columns = [column for column in servers[0].keys() if column in available_columns]
    if not insert_columns:
        return

    rows = [{column: server.get(column) for column in insert_columns} for server in servers]
    conn = sqlite3.connect(_path(db_path))
    cursor = conn.cursor()
    placeholders = ", ".join(f":{column}" for column in insert_columns)
    sql = f"INSERT INTO server_instances ({', '.join(insert_columns)}) VALUES ({placeholders})"
    cursor.executemany(sql, rows)
    conn.commit()
    conn.close()


def backup_database(db_path: str | Path, backup_dir: str | Path) -> str | None:
    source_path = _path(db_path)
    if not source_path.exists():
        return None

    backup_root = _path(backup_dir)
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_root / f"asr_tasks_test_backup_{timestamp}.db"
    shutil.copy(source_path, backup_path)
    return str(backup_path)


def reset_directory(path: str | Path):
    directory = _path(path)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)


def run_alembic_upgrade(backend_dir: str | Path, db_path: str | Path) -> None:
    backend_path = _path(backend_dir)
    database_path = _path(db_path)

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_path,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if database_path.exists():
            database_path.unlink()
        output_result(False, f"数据库迁移失败: {proc.stderr.strip()}")


def summarize_tasks(db_path: str | Path, latest_limit: int = 5) -> dict:
    if not table_exists(db_path, "tasks"):
        return {
            "total": 0,
            "by_status": {},
            "latest": [],
            "result_path_count": 0,
        }

    conn = sqlite3.connect(_path(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    total = cursor.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()[0]
    status_rows = cursor.execute(
        "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status ORDER BY status"
    ).fetchall()
    latest_rows = cursor.execute(
        """
        SELECT
            task_id,
            status,
            progress,
            assigned_server_id,
            result_path,
            created_at,
            completed_at
        FROM tasks
        ORDER BY COALESCE(completed_at, created_at) DESC, created_at DESC
        LIMIT ?
        """,
        (latest_limit,),
    ).fetchall()
    result_path_count = cursor.execute(
        "SELECT COUNT(*) AS count FROM tasks WHERE result_path IS NOT NULL AND TRIM(result_path) != ''"
    ).fetchone()[0]
    conn.close()

    return {
        "total": total,
        "by_status": {row["status"]: row["count"] for row in status_rows},
        "latest": [dict(row) for row in latest_rows],
        "result_path_count": result_path_count,
    }


def build_summary_text(servers: dict, database_files: dict, results: dict, temp: dict, uploads: dict, tasks: dict, estimated_savings: dict) -> str:
    parts = [
        f"当前共检测到 {servers['count']} 台服务器配置，任务总数 {tasks['total']} 条。",
        f"数据库相关文件 {database_files['count']} 个，总大小 {database_files['total_size_human']}。",
        f"results 目录 {results['file_count']} 个文件，共 {results['total_size_human']}；temp 目录 {temp['file_count']} 个文件，共 {temp['total_size_human']}。",
        f"按当前重置逻辑，预计可释放约 {estimated_savings['full_reset_human']}；仅重建数据库预计可回收 {estimated_savings['database_only_human']}。",
    ]
    if uploads["file_count"] > 0:
        parts.append(
            f"uploads 当前占用 {uploads['total_size_human']}，默认不会清理；只有显式指定 --clear-uploads 才会纳入释放空间估算。"
        )
    return " ".join(parts)


def evaluate_backend_data(
    backend_dir: str | Path,
    db_path: str | Path,
    results_dir: str | Path,
    temp_dir: str | Path,
    uploads_dir: str | Path,
    include_uploads: bool,
) -> dict:
    backend_path = _path(backend_dir)
    database_files = summarize_database_files(db_path)

    db_corrupt = False
    try:
        servers = summarize_servers(db_path)
    except sqlite3.DatabaseError:
        db_corrupt = True
        servers = {"count": 0, "by_status": {}, "items": [],
                   "error": "数据库损坏，无法读取服务器配置"}

    try:
        tasks = summarize_tasks(db_path)
    except sqlite3.DatabaseError:
        db_corrupt = True
        tasks = {"total": 0, "by_status": {}, "latest": [],
                 "result_path_count": 0, "error": "数据库损坏，无法读取任务数据"}

    results = summarize_directory(results_dir)
    temp = summarize_directory(temp_dir)
    uploads = summarize_directory(uploads_dir)

    database_only_bytes = database_files["total_size_bytes"]
    full_reset_bytes = database_only_bytes + results["total_size_bytes"] + temp["total_size_bytes"]
    if include_uploads:
        full_reset_bytes += uploads["total_size_bytes"]

    estimated_savings = {
        "database_only_bytes": database_only_bytes,
        "database_only_human": format_bytes(database_only_bytes),
        "full_reset_bytes": full_reset_bytes,
        "full_reset_human": format_bytes(full_reset_bytes),
        "includes": ["database_files", "results", "temp"] + (["uploads"] if include_uploads else []),
        "note": "数据库会被重建，因此实际净释放空间通常略低于 full_reset_bytes。",
    }

    result = {
        "dry_run": True,
        "backend_dir": str(backend_path),
        "database_files": database_files,
        "servers": servers,
        "tasks": tasks,
        "results": results,
        "temp": temp,
        "uploads": uploads,
        "estimated_savings": estimated_savings,
        "summary": build_summary_text(servers, database_files, results, temp, uploads, tasks, estimated_savings),
    }
    if db_corrupt:
        result["database_corrupt"] = True
        result["summary"] = (
            "⚠️ 数据库文件已损坏，无法读取现有数据。"
            "建议执行重置（不带 --dry-run）以重建数据库。 "
            + result["summary"]
        )
    return result


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="测试前数据库重置工具")
    parser.add_argument("--dry-run", action="store_true", help="仅检测和评估当前backend data状态，不执行任何清理或迁移")
    parser.add_argument("--no-backup", action="store_true", help="跳过数据库备份")
    parser.add_argument("--skip-seed-servers", action="store_true", help="不插入默认测试服务器数据（仅在--reset-servers时生效）")
    parser.add_argument("--clear-uploads", action="store_true", default=False, help="清空uploads目录下的音视频文件（默认保留，需要二次确认）")
    parser.add_argument("--reset-servers", action="store_true", help="重置服务器节点配置（默认保留已有服务器）")
    parser.add_argument("--force", action="store_true", help="跳过所有确认提示，强制执行")
    args = parser.parse_args(argv)

    backend_dir = _path(BACKEND_DIR)
    backup_dir = _path(BACKUP_DIR)
    db_path = _path(DB_PATH)
    data_dir = backend_dir / "data"
    results_dir = data_dir / "results"
    uploads_dir = data_dir / "uploads"
    temp_dir = data_dir / "temp"

    if not (backend_dir / "alembic.ini").exists():
        output_result(False, "backend目录结构不正确，找不到 alembic.ini")

    data_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        output_result(
            True,
            "dry-run 评估完成，未执行任何清理",
            evaluate_backend_data(
                backend_dir=backend_dir,
                db_path=db_path,
                results_dir=results_dir,
                temp_dir=temp_dir,
                uploads_dir=uploads_dir,
                include_uploads=args.clear_uploads,
            ),
        )

    in_use = check_database_in_use(db_path)
    if in_use:
        output_result(False, f"无法重置: {in_use}")

    result_data = {}
    servers_backup = []
    had_existing_database = db_path.exists()

    if had_existing_database and not args.reset_servers:
        try:
            servers_backup = export_servers(db_path)
            result_data["servers_preserved"] = len(servers_backup)
        except sqlite3.DatabaseError:
            servers_backup = []
            result_data["servers_preservation_skipped"] = "数据库损坏，无法导出服务器配置，将创建空库"
        except Exception as e:
            output_result(False, f"导出服务器配置失败: {str(e)}")

    if not args.no_backup:
        try:
            backup_path = backup_database(db_path, backup_dir)
            if backup_path:
                result_data["backup_path"] = backup_path
            else:
                result_data["backup_skipped"] = True
        except Exception as e:
            output_result(False, f"数据库备份失败: {str(e)}")

    try:
        reset_directory(results_dir)

        if args.clear_uploads:
            if not args.force:
                upload_count = sum(len(files) for _, _, files in os.walk(uploads_dir)) if uploads_dir.exists() else 0
                total_size = sum(file_path.stat().st_size for file_path in uploads_dir.rglob("*") if file_path.is_file()) if uploads_dir.exists() else 0
                size_mb = round(total_size / 1024 / 1024, 2)
                confirm = input(f"⚠️  你确定要删除uploads目录下的 {upload_count} 个文件（共 {size_mb} MB）吗？此操作不可逆！(y/N): ")
                if confirm.lower() not in {"y", "yes"}:
                    output_result(False, "已取消删除uploads目录操作")

            reset_directory(uploads_dir)
            result_data["uploads_cleared"] = True

        reset_directory(temp_dir)
    except Exception as e:
        output_result(False, f"清理测试文件失败: {str(e)}")

    try:
        for suffix in ("", "-wal", "-shm", "-journal"):
            target = Path(f"{db_path}{suffix}")
            if target.exists():
                target.unlink()
        run_alembic_upgrade(backend_dir, db_path)
        result_data["database_recreated"] = True
    except Exception as e:
        output_result(False, f"数据库重置失败: {str(e)}")

    if not args.reset_servers and servers_backup:
        try:
            import_servers(db_path, servers_backup)
        except Exception as e:
            output_result(False, f"恢复服务器配置失败: {str(e)}")

    if not args.skip_seed_servers and args.reset_servers:
        try:
            conn = sqlite3.connect(db_path)
            conn.executescript(TEST_DATA_SQL)
            conn.commit()
            conn.close()
            result_data["seed_data_inserted"] = True
        except Exception as e:
            output_result(False, f"插入测试数据失败: {str(e)}")
    else:
        result_data["seed_data_inserted"] = False

    output_result(
        True,
        "测试数据库重置完成，已准备好干净测试环境",
        result_data
    )


if __name__ == "__main__":
    main()