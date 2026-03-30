#!/usr/bin/env bash
# ASR Task Manager - 一键启动脚本
# 用法: bash start.sh [--no-frontend]
# 环境变量:
#   ASR_NO_RELOAD=1   关闭 uvicorn --reload（受限环境下推荐）
#   ASR_BIND_HOST=127.0.0.1  指定前后端绑定地址（默认 0.0.0.0）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
FRONTEND_DIR="$SCRIPT_DIR/frontend"
RUNTIME_DIR="$SCRIPT_DIR/../../.runtime"
mkdir -p "$RUNTIME_DIR"

BACKEND_LOG="$RUNTIME_DIR/backend.err.log"
BACKEND_OUT="$RUNTIME_DIR/backend.out.log"
FRONTEND_LOG="$RUNTIME_DIR/frontend.err.log"
FRONTEND_OUT="$RUNTIME_DIR/frontend.out.log"
PID_FILE="$RUNTIME_DIR/pids.txt"

NO_FRONTEND=0
if [[ "${1:-}" == "--no-frontend" ]]; then
  NO_FRONTEND=1
fi
NO_RELOAD="${ASR_NO_RELOAD:-0}"
BIND_HOST="${ASR_BIND_HOST:-0.0.0.0}"

EXIT_CODE=0

echo "========================================="
echo "  ASR Task Manager - 启动中..."
echo "========================================="

# ------ 依赖预检 ------
echo "[0/3] 检查依赖..."
MISSING=()
if ! command -v python &>/dev/null; then
  MISSING+=("python")
fi
if ! python -c "import uvicorn" &>/dev/null; then
  MISSING+=("uvicorn (pip install uvicorn)")
fi
if ! command -v curl &>/dev/null; then
  MISSING+=("curl")
fi
if [[ $NO_FRONTEND -eq 0 ]]; then
  if ! command -v npx &>/dev/null; then
    MISSING+=("npx (install Node.js)")
  fi
fi
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo ""
  echo "  ✗ 缺少必要依赖:"
  for dep in "${MISSING[@]}"; do
    echo "    - $dep"
  done
  echo ""
  echo "  请先安装上述依赖后重试。"
  echo "========================================="
  exit 1
fi
echo "      依赖检查通过 ✓"

# ------ 数据库迁移 ------
echo "[1/4] 数据库迁移..."
cd "$BACKEND_DIR"
if python -c "import alembic" &>/dev/null && [[ -f "alembic.ini" ]]; then
  python -m alembic upgrade head 2>"$RUNTIME_DIR/alembic.log"
  if [[ $? -eq 0 ]]; then
    echo "      数据库迁移完成 ✓"
  else
    echo "  ✗ 数据库迁移失败，请查看日志: $RUNTIME_DIR/alembic.log"
    exit 1
  fi
else
  echo "      跳过迁移（alembic 未安装或无 alembic.ini）"
fi

# ------ 后端 ------
echo "[2/4] 启动后端 (uvicorn)..."
UVICORN_CMD=(python -m uvicorn app.main:app --host "$BIND_HOST" --port 8000)
if [[ "$NO_RELOAD" != "1" ]]; then
  UVICORN_CMD+=(--reload)
fi
nohup "${UVICORN_CMD[@]}" \
  >"$BACKEND_OUT" 2>"$BACKEND_LOG" &
BACKEND_PID=$!
echo "backend=$BACKEND_PID" > "$PID_FILE"
echo "      后端 PID: $BACKEND_PID"

# ------ 前端 ------
if [[ $NO_FRONTEND -eq 0 ]]; then
  echo "[3/4] 启动前端 (vite)..."
  cd "$FRONTEND_DIR"
  nohup npx vite --host "$BIND_HOST" --port 5173 \
    >"$FRONTEND_OUT" 2>"$FRONTEND_LOG" &
  FRONTEND_PID=$!
  echo "frontend=$FRONTEND_PID" >> "$PID_FILE"
  echo "      前端 PID: $FRONTEND_PID"
else
  echo "[3/4] 跳过前端 (--no-frontend)"
fi

# ------ 健康检查 ------
echo "[4/4] 等待服务就绪..."
sleep 4

BACKEND_OK=0
for i in $(seq 1 10); do
  if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    BACKEND_OK=1
    break
  fi
  sleep 1
done

if [[ $BACKEND_OK -eq 1 ]]; then
  echo ""
  echo "  ✓ 后端就绪: http://127.0.0.1:8000"
  echo "    健康检查: $(curl -s http://127.0.0.1:8000/health)"
  echo "    API 文档: http://127.0.0.1:8000/docs"
else
  echo ""
  echo "  ✗ 后端启动超时，请查看日志: $BACKEND_LOG"
  EXIT_CODE=1
fi

if [[ $NO_FRONTEND -eq 0 ]]; then
  sleep 1
  if curl -sf http://127.0.0.1:5173 >/dev/null 2>&1; then
    echo "  ✓ 前端就绪: http://127.0.0.1:5173"
  else
    echo "  ✗ 前端启动超时，请查看日志: $FRONTEND_LOG"
    EXIT_CODE=1
  fi
fi

echo ""
echo "  日志文件:"
echo "    后端: $BACKEND_LOG"
echo "    前端: $FRONTEND_LOG"
echo ""
echo "  停止服务: bash $(dirname "$0")/stop.sh"
echo "========================================="

exit $EXIT_CODE
