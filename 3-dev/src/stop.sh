#!/usr/bin/env bash
# ASR Task Manager - 一键停止脚本
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUNTIME_DIR="$SCRIPT_DIR/../../.runtime"
PID_FILE="$RUNTIME_DIR/pids.txt"

EXPECTED_PATTERNS=("uvicorn" "vite" "node")

echo "========================================="
echo "  ASR Task Manager - 停止中..."
echo "========================================="

_is_our_process() {
  local pid="$1"
  local cmdline
  cmdline=$(ps -p "$pid" -o args= 2>/dev/null || true)
  if [[ -z "$cmdline" ]]; then
    return 1
  fi
  for pat in "${EXPECTED_PATTERNS[@]}"; do
    if [[ "$cmdline" == *"$pat"* ]]; then
      return 0
    fi
  done
  return 1
}

_kill_pid() {
  local pid="$1" label="$2"
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "  - $label (PID $pid) 已不存在"
    return 0
  fi
  if ! _is_our_process "$pid"; then
    echo "  ⚠ $label (PID $pid) 已被其他进程复用，跳过"
    return 0
  fi
  kill "$pid" 2>/dev/null && echo "  ✓ 已停止 $label (PID $pid)" || true
  for _ in $(seq 1 4); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -9 "$pid" 2>/dev/null || true
  echo "  ✓ 已强杀 $label (PID $pid)"
}

_kill_by_port() {
  local port="$1" label="$2"
  local pids
  pids=$(lsof -ti :"$port" 2>/dev/null || true)
  if [[ -z "$pids" ]]; then
    return 0
  fi
  for pid in $pids; do
    if _is_our_process "$pid"; then
      kill "$pid" 2>/dev/null && echo "  ✓ 已停止 $label (端口 $port, PID $pid)" || true
    fi
  done
}

if [[ -f "$PID_FILE" ]]; then
  while IFS='=' read -r name pid; do
    [[ -z "$pid" ]] && continue
    _kill_pid "$pid" "$name"
  done < "$PID_FILE"
  rm -f "$PID_FILE"
fi

# 无论 PID 文件是否存在，都做端口兜底清理
_kill_by_port 8000 "后端"
_kill_by_port 5173 "前端"

echo ""
echo "  所有服务已停止。"
echo "========================================="
