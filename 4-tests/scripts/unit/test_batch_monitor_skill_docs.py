"""Regression tests for active skill docs that define batch completion."""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
BATCH_MONITOR_SKILL = REPO_ROOT / "6-skills" / "funasr-task-manager-batch-monitor" / "SKILL.md"
LOCAL_BATCH_SKILL = REPO_ROOT / "6-skills" / "funasr-task-manager-local-batch-transcribe" / "SKILL.md"
WEB_E2E_SKILL = REPO_ROOT / "6-skills" / "funasr-task-manager-web-e2e" / "SKILL.md"
EMERGENCY_STOP_SKILL = REPO_ROOT / "6-skills" / "funasr-task-manager-emergency-stop" / "SKILL.md"
SERVER_BENCHMARK_SKILL = REPO_ROOT / "6-skills" / "funasr-task-manager-server-benchmark" / "SKILL.md"
LEGACY_CONDITION = "succeeded + failed == total"


@pytest.mark.unit
def test_batch_monitor_skill_uses_complete_semantics_with_canceled():
    content = BATCH_MONITOR_SKILL.read_text(encoding="utf-8")

    assert LEGACY_CONDITION not in content
    assert "stats.is_complete" in content or "succeeded + failed + canceled" in content


@pytest.mark.unit
def test_web_e2e_skill_counts_canceled_tasks_as_terminal_batch_completion():
    content = WEB_E2E_SKILL.read_text(encoding="utf-8")

    assert LEGACY_CONDITION not in content
    assert "succeeded + failed + canceled" in content or "is_complete" in content


@pytest.mark.unit
def test_local_batch_skill_defines_cancel_flow_for_active_segments():
    content = LOCAL_BATCH_SKILL.read_text(encoding="utf-8")

    assert "用户主动取消/中止批次" in content
    assert "task cancel {task_id}" in content
    assert "TRANSCRIBING" in content
    assert "僵尸 segment" in content


@pytest.mark.unit
def test_local_batch_skill_cleans_stale_monitors_and_exclusive_locks():
    content = LOCAL_BATCH_SKILL.read_text(encoding="utf-8")

    assert "stale monitor 清理" in content
    assert "runtime/agent-local-batch/monitors" in content
    assert "archive/monitors" in content
    assert "asr-exclusive" in content
    assert "10095" in content and "10096" in content and "10097" in content


@pytest.mark.unit
def test_batch_monitor_skill_requires_monitor_state_finalization():
    content = BATCH_MONITOR_SKILL.read_text(encoding="utf-8")

    assert "runtime/agent-local-batch/monitors/{batch_id}.json" in content
    assert "monitor state 收尾" in content
    assert "archive/monitors" in content
    assert "不得继续心跳或轮询" in content


@pytest.mark.unit
def test_emergency_stop_skill_requires_dry_run_confirm_and_active_slot_verification():
    content = EMERGENCY_STOP_SKILL.read_text(encoding="utf-8")

    assert "admin active-slots" in content
    assert "admin emergency-stop" in content
    assert "--confirm" in content
    assert "dry-run" in content
    assert "total_active_slots == 0" in content
    assert "不直接改数据库" in content


@pytest.mark.unit
def test_server_benchmark_skill_requires_verified_project_cli_entrypoint():
    content = SERVER_BENCHMARK_SKILL.read_text(encoding="utf-8")

    assert "项目 CLI 入口自检" in content
    assert "asr-cli 0.4.28" in content
    assert ".venv" in content
    assert "不得混用系统 Python" in content
