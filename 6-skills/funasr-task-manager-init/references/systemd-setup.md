# systemd 后端服务安装参考

Phase 8 的详细操作步骤和模板。**仅适用于 Unicorn（uv）环境安装的 Python 后端**；Docker 安装应使用 `docker compose` 的 `restart: always` 策略，不需要 systemd。

## Step 1：前置检查

```bash
uname -s            # 必须返回 Linux
which systemctl     # 必须存在
```

还需确认安装方式：
- 如果后端运行在 Docker 容器中 → **不应使用本流程**，提示用户在 `docker-compose.yml` 中设置 `restart: always`
- 如果后端运行在 Unicorn venv 中 → 继续

## Step 2：收集参数

Agent 自动检测并向用户确认。**每个候选 Python 都必须通过 `import uvicorn` 验证才能选定**——路径存在 ≠ 可用。

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
RUN_USER="$(whoami)"
PORT=8000
PYTHON_PATH=""

# 按优先级逐个验证（路径存在 + import uvicorn 成功才选定）
for candidate in \
  "$REPO_ROOT/3-dev/src/backend/.venv/bin/python" \
  "$REPO_ROOT/.venv/bin/python" \
  "$(which python3 2>/dev/null)"; do
  if [ -x "$candidate" ] && "$candidate" -c "import uvicorn" 2>/dev/null; then
    PYTHON_PATH="$candidate"
    break
  fi
done

if [ -z "$PYTHON_PATH" ]; then
  echo "❌ 所有候选 Python 均未安装 uvicorn，无法创建 systemd 服务"
  echo "   请先安装: pip install uvicorn"
  exit 1
fi

echo "✅ 已选定 Python: $PYTHON_PATH"
"$PYTHON_PATH" -c "import uvicorn; print(f'   uvicorn {uvicorn.__version__}')"
```

> **⚠️ 关键规则：`import uvicorn` 验证失败的路径必须跳过，不得使用。所有候选均失败时必须中止，不可生成 service 文件。**

**向用户询问**：

> 将后端注册为 systemd 服务，请确认以下信息：
>
> | 参数 | 检测值 | 确认？ |
> |------|--------|--------|
> | 项目路径 | `{REPO_ROOT}` | |
> | Python 解释器 | `{PYTHON_PATH}`（已验证 uvicorn 可用） | |
> | 监听端口 | `8000` | |
> | 运行用户 | `{RUN_USER}` | |
>
> 按回车使用检测值，或输入新值覆盖。

**如果用户覆盖了 Python 路径，必须对新路径重新验证**：

```bash
# 用户提供了自定义 Python 路径时
if ! "$USER_PYTHON_PATH" -c "import uvicorn" 2>/dev/null; then
  echo "❌ 该 Python ($USER_PYTHON_PATH) 未安装 uvicorn，无法使用"
  echo "   请提供包含 uvicorn 的 Python 路径，或先安装: $USER_PYTHON_PATH -m pip install uvicorn"
  # 必须中止，不得继续生成 service 文件
  exit 1
fi
PYTHON_PATH="$USER_PYTHON_PATH"
```

> **覆盖路径与自动检测路径适用同一规则：`import uvicorn` 不通过 → 不得写入 service 文件。**

## Step 3：service 文件模板

```ini
[Unit]
Description=FunASR Task Manager Backend
After=network.target

[Service]
Type=exec
User={RUN_USER}
WorkingDirectory={REPO_ROOT}/3-dev/src/backend
Environment=ASR_PROJECT_ROOT={REPO_ROOT}
ExecStart={PYTHON_PATH} -m uvicorn app.main:app --host 0.0.0.0 --port {PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**写入前必须向用户展示完整内容并请求确认**：

```bash
# ⚠️ 需要 sudo 权限
sudo tee /etc/systemd/system/funasr-task-manager-backend.service > /dev/null << 'UNIT'
{替换后的 unit 文件内容}
UNIT
```

如果已有同名 service 文件：

```bash
# 展示差异
diff /etc/systemd/system/funasr-task-manager-backend.service <(cat << 'UNIT'
{新的 unit 文件内容}
UNIT
)
```

让用户选择：覆盖 / 跳过 / 查看差异后决定。

## Step 4：启用并启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable funasr-task-manager-backend
sudo systemctl start funasr-task-manager-backend
```

## Step 5：验证

```bash
sleep 5
systemctl is-active funasr-task-manager-backend
curl -sf http://127.0.0.1:{PORT}/health
```

验证通过后输出：

```
✅ 后端已注册为 systemd 服务

  服务名:   funasr-task-manager-backend
  状态:     active (running)
  健康检查: ✅ 通过
  端口:     {PORT}
  自启动:   已启用
  日志:     journalctl -u funasr-task-manager-backend -f

  常用命令:
  - 查看状态: sudo systemctl status funasr-task-manager-backend
  - 查看日志: journalctl -u funasr-task-manager-backend --since today
  - 重启服务: sudo systemctl restart funasr-task-manager-backend
  - 停止服务: sudo systemctl stop funasr-task-manager-backend
```

验证失败时：

```bash
# 检查失败原因
sudo systemctl status funasr-task-manager-backend
journalctl -u funasr-task-manager-backend --no-pager -n 30
```

常见失败原因和解决方案：

| 原因 | 日志特征 | 解决方案 |
|------|---------|---------|
| Python 路径错误 | `ExecStart: not found` | 修正 service 文件中的 Python 路径 |
| 端口被占用 | `Address already in use` | 停止占用进程或修改 Port |
| 权限不足 | `Permission denied` | 检查 User 和 WorkingDirectory 权限 |
| 依赖缺失 | `ModuleNotFoundError` | 检查 venv 路径或安装依赖 |
| 数据库锁 | `database is locked` | 确保无其他后端实例在运行 |

## 卸载

```bash
sudo systemctl stop funasr-task-manager-backend
sudo systemctl disable funasr-task-manager-backend
sudo rm /etc/systemd/system/funasr-task-manager-backend.service
sudo systemctl daemon-reload
```

## macOS 替代方案

macOS 不支持 systemd。替代方案：

1. **开发环境**：使用 `nohup` 或终端 multiplexer（tmux/screen）
   ```bash
   cd 3-dev/src/backend
   nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 &
   ```

2. **生产环境**：使用 launchd
   ```bash
   # 创建 plist 文件（需要用户自行配置）
   ~/Library/LaunchAgents/com.funasr-task-manager.backend.plist
   ```
