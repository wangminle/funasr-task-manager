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

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (aiosqlite) + SQLAlchemy 2.0 |
| 任务队列 | Dramatiq + Redis |
| ASR 对接 | WebSocket (websockets) |
| 前端 | Vue 3 + Vite + Element Plus |
| 监控 | Prometheus + Grafana |
| 部署 | Docker Compose |

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 20+
- Redis
- ffmpeg（用于音频元信息提取）

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

### Docker 一键部署

```bash
cd 3-dev/src/backend

# 启动全部服务（web + redis + prometheus + grafana）
docker-compose up -d
```

服务启动后访问：
- API 文档：http://localhost:8000/docs
- 前端界面：http://localhost:5173（开发模式）或 http://localhost:80（Docker 部署）
- Prometheus：http://localhost:9090
- Grafana：http://localhost:3001（默认账号 admin/admin）

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/files/upload` | 上传音频文件 |
| GET | `/api/v1/files/{file_id}` | 查询文件元信息 |
| POST | `/api/v1/tasks` | 创建转写任务（支持批量） |
| GET | `/api/v1/tasks` | 任务列表（分页） |
| GET | `/api/v1/tasks/{task_id}` | 任务详情 |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/api/v1/tasks/{task_id}/result` | 下载转写结果（json/txt/srt） |
| GET | `/api/v1/tasks/{task_id}/progress` | SSE 实时进度 |
| POST | `/api/v1/servers` | 注册 ASR 服务器 |
| GET | `/api/v1/servers` | 服务器列表 |
| DELETE | `/api/v1/servers/{server_id}` | 注销服务器 |
| GET | `/metrics` | Prometheus 指标 |

## 项目结构

```
funasr-task-manager/
├── 1-discussion/          # 调研讨论文档
├── 2-design/              # 设计文档 + UI 设计稿
├── 3-dev/src/
│   ├── backend/           # FastAPI 后端
│   │   ├── app/           # 应用代码
│   │   ├── alembic/       # 数据库迁移
│   │   ├── config/        # 配置文件
│   │   └── docker-compose.yaml
│   └── frontend/          # Vue 3 前端
├── 4-tests/
│   ├── scripts/           # 测试脚本
│   │   ├── unit/          # 单元测试 (133 tests)
│   │   ├── integration/   # 集成测试 (27 tests)
│   │   ├── e2e/           # 端到端测试
│   │   └── load/          # 压力测试 (Locust)
│   └── reports/           # 测试报告
└── README.md
```

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
