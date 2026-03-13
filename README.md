# ASR 任务管理器（中转适配层）

集中式离线 ASR 任务管理系统，对接 FunASR 服务器集群，提供统一的文件上传、任务调度、进度追踪和结果下载能力。

## 功能特性

- **文件管理**：支持 WAV/MP3/MP4/FLAC 等多格式上传，自动提取音频元信息（时长、采样率、编码等）
- **任务调度**：LPT（最长处理时间优先）+ 最早完工时间调度算法，支持单任务和批量任务创建
- **多服务器管理**：ASR 服务器注册/注销、心跳检测、自动探测协议版本（新版/旧版 FunASR）
- **实时进度**：SSE（Server-Sent Events）实时推送任务进度和 ETA 估算
- **容错机制**：断路器模式（CLOSED/OPEN/HALF_OPEN）、指数退避重试、服务器自动轮转
- **安全认证**：API Token 认证、用户资源隔离、并发/带宽/日量三维限流
- **可观测性**：structlog 结构化日志、Prometheus 指标暴露、Grafana 仪表盘、6 条告警规则
- **回调通知**：Outbox 模式 + HMAC 签名，确保事件可靠投递
- **CLI 工具**：完整命令行界面（Typer + Rich），支持一键转写、批量上传、任务管理、配置持久化，三种输出格式（table/json/text），适配自动化脚本和 AI Agent 调用

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (aiosqlite) / PostgreSQL (asyncpg, 可选) + SQLAlchemy 2.0 |
| 任务调度 | 进程内 BackgroundTaskRunner（asyncio）/ Dramatiq + Redis（可选多实例） |
| ASR 对接 | WebSocket (websockets) |
| 前端 | Vue 3 + Vite + Element Plus + ECharts |
| 监控 | Prometheus + Grafana + Alertmanager |
| CLI | Typer + Rich + httpx |
| 部署 | Docker Compose |

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 20+
- ffmpeg（用于音频元信息提取）

> **注意**：当前版本使用进程内 `BackgroundTaskRunner`（基于 asyncio）执行任务调度，无需外部 Redis。如需多实例水平扩展部署，需迁移至 Dramatiq + Redis 方案（代码中已预留依赖和迁移路径）。

### 后端启动

```bash
cd 3-dev/src/backend

# 安装依赖
pip install -e ".[dev]"

# 数据库迁移
alembic upgrade head

# 启动服务
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 前端启动

```bash
cd 3-dev/src/frontend

npm install
npm run dev
```

### 一键启停脚本（推荐）

项目提供了一键启停脚本，用于在开发环境中管理前后端服务。**Windows 和 Linux/macOS 各有对应版本**：

| 操作系统 | 启动 | 停止 |
|----------|------|------|
| **Windows** (PowerShell) | `.\start.ps1` | `.\stop.ps1` |
| **Linux / macOS** (Bash) | `bash start.sh` | `bash stop.sh` |

> **Windows 用户注意**：请使用 PowerShell 版脚本（`.ps1`）。不要用 `bash start.sh`，因为 Windows 的 `bash` 命令会调用 WSL（Linux 子系统），WSL 中的依赖与 Windows 本地环境相互隔离，会导致依赖检查失败。

#### 启动服务

**Windows (PowerShell)：**

```powershell
cd 3-dev\src

# 启动前后端
.\start.ps1

# 仅启动后端（跳过前端）
.\start.ps1 -NoFrontend

# 关闭热重载 + 仅绑定本机
.\start.ps1 -NoReload -BindHost 127.0.0.1
```

**Linux / macOS (Bash)：**

```bash
cd 3-dev/src

# 启动前后端
bash start.sh

# 仅启动后端（跳过前端）
bash start.sh --no-frontend

# 关闭热重载 + 仅绑定本机
ASR_NO_RELOAD=1 ASR_BIND_HOST=127.0.0.1 bash start.sh
```

**启动流程（两个版本一致）：**

1. **依赖预检** — 检测 `python`、`uvicorn`、`curl`（以及前端需要的 `npx`），缺少任何依赖会提示并退出
2. **启动后端** — 后台运行 `uvicorn app.main:app`，默认监听 `0.0.0.0:8000`，开启 `--reload` 热重载
3. **启动前端** — 后台运行 `npx vite`，默认监听 `0.0.0.0:5173`
4. **健康检查** — 轮询 `http://127.0.0.1:8000/health`（最多 10 次，每次间隔 1 秒），确认后端就绪后检查前端可访问性

**参数/环境变量对照：**

| 功能 | PowerShell (`.ps1`) | Bash (`.sh`) |
|------|---------------------|--------------|
| 跳过前端 | `-NoFrontend` | `--no-frontend` |
| 关闭热重载 | `-NoReload` | `ASR_NO_RELOAD=1` |
| 绑定地址 | `-BindHost 127.0.0.1` | `ASR_BIND_HOST=127.0.0.1` |

> PowerShell 版同样支持环境变量 `ASR_NO_RELOAD` 和 `ASR_BIND_HOST`，参数和环境变量均可使用，参数优先级更高。

**日志与 PID 文件：**

启动后所有日志和 PID 信息保存在项目根目录的 `.runtime/` 下：

| 文件 | 说明 |
|------|------|
| `.runtime/pids.txt` | 记录后端/前端进程 PID，供停止脚本使用 |
| `.runtime/backend.out.log` | 后端标准输出 |
| `.runtime/backend.err.log` | 后端错误输出（排查问题首先查看此文件） |
| `.runtime/frontend.out.log` | 前端标准输出 |
| `.runtime/frontend.err.log` | 前端错误输出 |

#### 停止服务

**Windows (PowerShell)：**

```powershell
cd 3-dev\src
.\stop.ps1
```

**Linux / macOS (Bash)：**

```bash
cd 3-dev/src
bash stop.sh
```

**停止流程（两个版本一致）：**

1. **读取 PID 文件** — 解析 `.runtime/pids.txt`，逐一停止已记录的进程
2. **安全性校验** — 通过进程名/命令行关键字（`uvicorn`、`vite`、`node`）确认 PID 仍属于本项目，避免误杀已被系统复用的 PID
3. **优雅退出** — Bash 版先发送 `SIGTERM`，等待 2 秒后 `SIGKILL` 强杀；PowerShell 版使用 `Stop-Process -Force`
4. **端口兜底** — 无论 PID 文件是否存在，都会检查并清理占用 `8000`（后端）和 `5173`（前端）端口的相关进程（Bash 用 `lsof`，PowerShell 用 `Get-NetTCPConnection`）

> 即使 `.runtime/pids.txt` 丢失或手动删除，停止脚本也能通过端口扫描兜底停止服务。

### Docker 一键部署

```bash
cd 3-dev/src/backend

# 启动全部服务（web + redis + prometheus + alertmanager + grafana）
docker-compose up -d

# 可选：使用 PostgreSQL 替代 SQLite（高并发场景）
# 在 3-dev/src/backend/ 目录下创建 .env 文件：
#   POSTGRES_PASSWORD=your_secure_password
#   ASR_DATABASE_URL=postgresql+asyncpg://asr:your_secure_password@postgres:5432/asr_tasks
docker compose --profile postgres up -d
# 首次启动后执行迁移: docker compose exec web alembic upgrade head
```

服务启动后访问：
- API 文档：http://localhost:8000/docs
- 前端界面：http://localhost:5173（开发模式）或 http://localhost:80（Docker 部署）
- Prometheus：http://localhost:9090
- Alertmanager：http://localhost:9093
- Grafana：http://localhost:3001（默认账号 admin/admin）

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/files/upload` | 上传音频文件 |
| GET | `/api/v1/files/{file_id}` | 查询文件元信息 |
| POST | `/api/v1/tasks` | 创建转写任务（支持批量） |
| GET | `/api/v1/tasks` | 任务列表（分页、状态筛选、关键词搜索） |
| GET | `/api/v1/tasks/{task_id}` | 任务详情 |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/api/v1/tasks/{task_id}/result` | 下载转写结果（json/txt/srt） |
| GET | `/api/v1/tasks/{task_id}/progress` | SSE 实时进度 |
| POST | `/api/v1/servers` | 注册 ASR 服务器 |
| GET | `/api/v1/servers` | 服务器列表 |
| DELETE | `/api/v1/servers/{server_id}` | 注销服务器 |
| GET | `/api/v1/stats` | 系统统计（节点/槽位/队列/成功率） |
| POST | `/api/v1/internal/alert-webhook` | Alertmanager 告警接收 |
| GET | `/metrics` | Prometheus 指标 |

## 项目结构

```
funasr-task-manager/
├── .runtime/              # 运行时目录（自动生成，已 gitignore）
│   ├── pids.txt           # 进程 PID 记录
│   ├── backend.out.log    # 后端标准输出
│   ├── backend.err.log    # 后端错误日志
│   ├── frontend.out.log   # 前端标准输出
│   └── frontend.err.log   # 前端错误日志
├── 1-discussion/          # 调研讨论文档
├── 2-design/              # 设计文档 + UI 设计稿
├── 3-dev/src/
│   ├── start.sh / start.ps1   # 一键启动 (Bash / PowerShell)
│   ├── stop.sh  / stop.ps1    # 一键停止 (Bash / PowerShell)
│   ├── backend/           # FastAPI 后端
│   │   ├── app/           # 应用代码
│   │   ├── cli/           # CLI 工具 (Typer)
│   │   ├── alembic/       # 数据库迁移
│   │   ├── config/        # 配置文件
│   │   └── docker-compose.yaml
│   └── frontend/          # Vue 3 前端
├── 4-tests/
│   ├── scripts/           # 测试脚本
│   │   ├── unit/          # 单元测试 (184 tests)
│   │   ├── integration/   # 集成测试 (34 tests)
│   │   ├── e2e/           # 端到端测试
│   │   └── load/          # 压力测试 (Locust)
│   └── reports/           # 测试报告
└── README.md
```

## CLI 工具

CLI 提供与 Web UI 完全对等的命令行操作能力，支持脚本化和 AI Agent 集成。

```bash
cd 3-dev/src/backend

# 查看帮助
python -m cli --help

# 一键转写（上传→创建任务→等待→下载结果）
python -m cli transcribe recording.wav --language zh --format srt --output-dir ./results/

# 批量转写
python -m cli transcribe *.wav --language zh --format json --output-dir ./results/

# 只提交不等待（适合异步管线）
python -m cli transcribe recording.wav --no-wait --output json

# 任务管理
python -m cli task list --status SUCCEEDED --page 1
python -m cli task info <task_id>
python -m cli task result <task_id> --format srt --save output.srt

# 系统状态
python -m cli health
python -m cli stats --output json

# 配置持久化
python -m cli config set server http://asr-server:8000
python -m cli config set api_key my-token
python -m cli config list
```

全局选项：`--server` / `--api-key` / `--output (table|json|text)` / `--quiet` / `--verbose`

## 测试

```bash
# 工作目录
cd 3-dev/src/backend

# 全量测试 + 覆盖率
python -m pytest "../../../4-tests/scripts/" -v --cov=app

# 仅单元测试
python -m pytest "../../../4-tests/scripts/unit/" -v

# 仅集成测试
python -m pytest "../../../4-tests/scripts/integration/" -v

# E2E 测试
python -m pytest "../../../4-tests/scripts/e2e/" -v

# 压力测试
locust -f "../../../4-tests/scripts/load/locustfile.py" --headless -u 50 -r 10 -t 5m

# 生成 HTML 报告
python -m pytest "../../../4-tests/scripts/" \
    --html="../../../4-tests/reports/full-report.html" --self-contained-html
```

## 认证

API 使用静态 Token 认证（开发阶段），通过 `X-API-Key` 请求头传递：

| Token | 用户 |
|-------|------|
| `dev-token-user1` | user1 |
| `dev-token-user2` | user2 |
| `dev-token-admin` | admin |

默认关闭认证（`_AUTH_ENABLED = False`），生产环境需通过配置启用。

## 设计文档

- [独立调研报告](2-design/2026-02-26-ASR任务中转适配层-独立调研报告.md)
- [项目实施方案（测试驱动验收）](2-design/2026-02-27-项目实施方案-测试驱动验收.md)
- [架构与技术选型调研](1-discussion/2026-02-26-ASR任务管理器-架构与技术选型调研报告.md)

## 许可证

内部项目，仅限授权使用。
