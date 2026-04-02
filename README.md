# ASR 任务管理器

集中式离线语音识别（ASR）任务管理系统，对接 FunASR 服务器集群，提供统一的文件上传、智能任务调度、实时进度追踪和多格式结果下载。支持 CLI / REST API / Web UI 三种使用方式。

## 你想做什么？

```
├── 转写 1 个文件            → python -m cli transcribe audio.mp4
├── 转写多个文件（自动并行）  → python -m cli transcribe *.wav --format txt
├── 只提交不等待              → python -m cli transcribe files --no-wait
├── 查看批次进度              → python -m cli task list --group <group_id>
├── 下载批次结果              → python -m cli task result --group <group_id> --format txt,srt
├── 管理 ASR 服务器           → python -m cli server list / probe / benchmark
├── 排查系统问题              → python -m cli doctor
└── API 集成开发              → 阅读下方 API 参考
```

> **注**: `pip install -e .` 后可使用 `asr-cli` 替代 `python -m cli`（二者等价）。开发环境推荐 `python -m cli`，生产环境推荐 `asr-cli`。

---

## 快速开始

### 环境要求

- Python 3.11+
- Node.js 20+（前端可选）
- ffmpeg / ffprobe（推荐安装：用于精确提取音频时长和本地 WAV 预处理；未安装时系统会按文件大小粗估时长，并直传原始文件给 FunASR 服务器解码）

### 安装 & 启动

```bash
cd 3-dev/src/backend

# 安装依赖
pip install -e ".[dev]"

# 数据库迁移
alembic upgrade head

# 启动后端
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

> **工作目录说明**
> 后端默认把数据库和上传/结果/临时文件写到相对路径 `./data/`。因此开发环境请始终在 `3-dev/src/backend` 目录下启动后端，或使用 `3-dev/src/start.sh` / `3-dev/src/start.ps1`。按推荐方式启动时，运行时数据目录为 `3-dev/src/backend/data/`。如果从仓库根目录直接启动 `uvicorn app.main:app`，会在仓库根目录额外生成顶层 `data/` 目录。

也可使用一键启停脚本（推荐开发环境）：

```powershell
# Windows PowerShell
cd 3-dev\src
.\start.ps1           # 启动前后端
.\start.ps1 -NoFrontend  # 仅后端
.\stop.ps1            # 停止所有
```

```bash
# Linux / macOS
cd 3-dev/src
bash start.sh                 # 启动前后端
bash start.sh --no-frontend   # 仅后端
bash stop.sh                  # 停止所有
```

### 30 秒上手：单文件转写

```bash
cd 3-dev/src/backend

# 上传并转写一个文件（自动上传→创建任务→等待→下载结果）
python -m cli transcribe recording.mp4 --language zh --format txt

# 指定输出目录
python -m cli transcribe meeting.wav --format srt --output-dir ./results/
```

---

## 批量转写指南

### CLI 批量模式

多文件时自动启用批量模式，一次性上传并创建所有任务，后端并行调度到多台 ASR 服务器：

```bash
# 转写目录下所有 wav 文件
python -m cli transcribe *.wav --format txt --output-dir ./results/

# 转写指定文件
python -m cli transcribe ep01.mp4 ep02.mp4 ep03.mp4 --format srt --output-dir ./srt/

# 强制批量模式（即使只有 1 个文件）
python -m cli transcribe single.wav --batch --format json

# 异步提交，不等待完成
python -m cli transcribe *.mp3 --no-wait --json-summary

# 完成后下载多种格式结果 + 生成摘要
python -m cli task result --group <group_id> --format txt,json,srt --output-dir ./results/
```

批量下载后的输出目录结构：

```
results/
├── batch-summary.json          # 批次摘要（所有任务状态和输出路径）
├── ep01_result.txt
├── ep01_result.srt
├── ep02_result.txt
├── ep02_result.srt
└── ...
```

### API 批量模式

一次请求提交多个任务：

```bash
# 1. 上传多个文件
curl -X POST http://localhost:8000/api/v1/files/upload -F "file=@ep01.wav"
# → {"file_id": "01JQXXX1..."}

curl -X POST http://localhost:8000/api/v1/files/upload -F "file=@ep02.wav"
# → {"file_id": "01JQXXX2..."}

# 2. 批量创建任务
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"file_id": "01JQXXX1...", "language": "zh"},
      {"file_id": "01JQXXX2...", "language": "zh"}
    ]
  }'

# 3. 查看批次进度
curl http://localhost:8000/api/v1/task-groups/<group_id>

# 4. 下载结果（zip 打包）
curl -o results.zip http://localhost:8000/api/v1/task-groups/<group_id>/results?format=zip
```

### 前端批量上传

Web UI 支持拖拽多文件上传和批量任务管理。访问 `http://localhost:5173` 打开前端界面。

---

## CLI 命令参考

所有命令的工作目录为 `3-dev/src/backend`，命令前缀为 `python -m cli`。

### 全局选项

| 选项 | 简写 | 环境变量 | 说明 | 默认值 |
|------|------|---------|------|--------|
| `--server` | `-s` | `ASR_API_SERVER` | API 服务地址 | `http://localhost:8000` |
| `--api-key` | `-k` | `ASR_API_KEY` | API 认证 Token | 无 |
| `--output` | `-o` | `ASR_OUTPUT_FORMAT` | 输出格式: `table`/`json`/`text` | `table` |
| `--quiet` | `-q` | — | 静默模式 | `false` |
| `--verbose` | `-v` | — | 调试模式 | `false` |
| `--timeout` | — | — | HTTP 超时(秒) | `30` |

配置可通过 `config set` 持久化，优先级：CLI 参数 > 环境变量 > 配置文件 > 默认值。

### transcribe — 一键转写

```bash
# 单文件
python -m cli transcribe audio.wav
python -m cli transcribe audio.wav --language zh --format srt --save output.srt

# 批量（自动并行调度到多台服务器）
python -m cli transcribe *.wav --format txt --output-dir ./results/

# 异步提交
python -m cli transcribe *.mp3 --no-wait --json-summary
```

| 选项 | 说明 |
|------|------|
| `--language, -l` | 识别语言，默认 `auto` |
| `--hotwords` | 热词列表，逗号分隔 |
| `--format, -f` | 结果格式: `json`/`txt`/`srt` |
| `--output-dir, -d` | 输出目录 |
| `--save` | 单文件时保存到指定路径 |
| `--no-wait` | 只提交不等待 |
| `--batch` | 强制批量模式 |
| `--download/--no-download` | 完成后是否下载 |
| `--json-summary` | 输出 JSON 格式摘要 |
| `--callback` | 完成后回调地址 |
| `--poll-interval` | 轮询间隔(秒)，默认 5 |
| `--timeout` | 等待超时(秒)，默认 3600 |

### task — 任务管理

```bash
# 创建任务
python -m cli task create <file_id1> <file_id2> --language zh

# 查看任务列表
python -m cli task list
python -m cli task list --status SUCCEEDED --page 1
python -m cli task list --group <group_id>    # 按批次筛选

# 查看任务详情
python -m cli task info <task_id>

# 下载单个任务结果
python -m cli task result <task_id> --format srt --save output.srt

# 下载整批结果（支持多格式同时导出）
python -m cli task result --group <group_id> --format txt,json,srt --output-dir ./results/

# 等待任务完成
python -m cli task wait <task_id1> <task_id2>
python -m cli task wait --group <group_id>    # 等待整批完成

# 取消任务
python -m cli task cancel <task_id>

# 删除批次
python -m cli task delete --group <group_id>

# 实时进度
python -m cli task progress <task_id>
```

### server — 节点管理

```bash
# 查看所有节点
python -m cli server list

# 注册新节点
python -m cli server register --id asr-01 --host 192.168.1.100 --port 10095 --protocol v2_new

# 探测节点连通性和能力
python -m cli server probe asr-01
python -m cli server probe asr-01 --level benchmark    # 含性能测试

# 全量性能基准测试
python -m cli server benchmark

# 更新节点配置
python -m cli server update asr-01 --max-concurrency 8
python -m cli server update asr-01 --name "高配服务器" --max-concurrency 12

# 删除节点
python -m cli server delete asr-01
```

探测级别说明：

| 级别 | 说明 |
|------|------|
| `connect_only` | 仅检查网络连通性 |
| `offline_light` | 连通性 + 离线转写能力探测（默认） |
| `twopass_full` | 完整双通道探测 |
| `benchmark` | 性能基准测试（含 RTF 计算） |

### 系统命令

```bash
# 健康检查
python -m cli health

# 系统统计
python -m cli stats

# 系统诊断（数据库、依赖、服务连通性）
python -m cli doctor

# Prometheus 指标
python -m cli metrics
```

### 配置管理

```bash
# 设置默认服务器地址
python -m cli config set server http://asr-server:8000

# 设置 API Key
python -m cli config set api_key my-token

# 查看当前配置
python -m cli config list
```

---

## API 参考

### 核心端点

| 方法 | 路径 | 说明 |
|------|------|------|
| **文件** | | |
| POST | `/api/v1/files/upload` | 上传音频文件 |
| GET | `/api/v1/files/{file_id}` | 查询文件元信息 |
| **任务** | | |
| POST | `/api/v1/tasks` | 创建转写任务（支持批量） |
| GET | `/api/v1/tasks` | 任务列表（分页/状态筛选/搜索/按批次） |
| GET | `/api/v1/tasks/{task_id}` | 任务详情 |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/api/v1/tasks/{task_id}/result?format=` | 下载结果（json/txt/srt） |
| GET | `/api/v1/tasks/{task_id}/progress` | SSE 实时进度 |
| **批次管理** | | |
| GET | `/api/v1/task-groups/{group_id}` | 批次概况 |
| GET | `/api/v1/task-groups/{group_id}/tasks` | 批次任务列表 |
| GET | `/api/v1/task-groups/{group_id}/results?format=` | 批次结果（txt/json/srt/zip） |
| DELETE | `/api/v1/task-groups/{group_id}` | 删除批次 |
| **节点管理** | | |
| POST | `/api/v1/servers` | 注册 ASR 节点 |
| GET | `/api/v1/servers` | 节点列表 |
| POST | `/api/v1/servers/{server_id}/probe` | 探测节点 |
| POST | `/api/v1/servers/benchmark` | 全量 benchmark |
| PATCH | `/api/v1/servers/{server_id}` | 更新节点配置 |
| DELETE | `/api/v1/servers/{server_id}` | 注销节点 |
| **系统** | | |
| GET | `/health` | 健康检查 |
| GET | `/api/v1/stats` | 系统统计 |
| GET | `/api/v1/diagnostics` | 系统诊断 |
| GET | `/metrics` | Prometheus 指标 |

### curl 示例

```bash
# 上传文件
curl -X POST http://localhost:8000/api/v1/files/upload \
  -F "file=@recording.wav"

# 创建单个任务
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"items": [{"file_id": "01JQXXX...", "language": "zh"}]}'

# 创建批量任务（含回调）
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"file_id": "FILE_ID_1", "language": "zh"},
      {"file_id": "FILE_ID_2", "language": "zh", "options": {"hotwords": "FunASR,转写"}}
    ],
    "callback": {"url": "https://your-server.com/webhook", "secret": "your-hmac-key"}
  }'

# 查看批次状态
curl http://localhost:8000/api/v1/task-groups/<group_id>
# → {"task_group_id": "...", "total": 5, "succeeded": 3, "failed": 0, ...}

# 下载转写结果（txt 格式）
curl http://localhost:8000/api/v1/tasks/<task_id>/result?format=txt

# 下载批次 zip
curl -o results.zip http://localhost:8000/api/v1/task-groups/<group_id>/results?format=zip

# 探测节点
curl -X POST http://localhost:8000/api/v1/servers/asr-01/probe?level=offline_light

# 系统诊断
curl http://localhost:8000/api/v1/diagnostics
```

---

## 多服务器配置

### 工作原理

ASR 任务管理器支持注册多台 FunASR 服务器，使用 LPT（最长处理时间优先）+ 最早完工时间调度算法，将任务智能分配到最优节点实现并行处理。

### 配置流程

```bash
# 1. 注册节点
python -m cli server register --id asr-gpu-01 --host 10.0.0.1 --port 10095 --protocol v2_new --max-concurrency 4
python -m cli server register --id asr-gpu-02 --host 10.0.0.2 --port 10095 --protocol v2_new --max-concurrency 8

# 2. 探测连通性和能力
python -m cli server probe asr-gpu-01
python -m cli server probe asr-gpu-02

# 3. 性能基准测试（自动计算 RTF 并更新调度权重）
python -m cli server benchmark

# 4. 查看节点状态
python -m cli server list
```

### 调度原理

| 概念 | 说明 |
|------|------|
| **RTF**（Real-Time Factor） | 处理1秒音频需要的时间。RTF=0.1 表示10x加速 |
| **LPT 调度** | 优先将最长文件分配给最快的服务器 |
| **最早完工时间** | 考虑当前队列负载，选择预估最早完成的节点 |
| **自动探测** | 服务器协议版本（v1_old/v2_new）自动探测 |
| **断路器** | CLOSED→OPEN→HALF_OPEN 三态切换，故障服务器自动隔离 |

---

## 环境诊断

当系统出现异常时，使用 `doctor` 命令排查：

```bash
python -m cli doctor
```

输出示例：

```
┌──────────────────────────────────────────────────────┐
│               系统诊断报告                             │
├─────────────────────┬──────┬────────────────────────┤
│ 检查项              │ 状态 │ 说明                    │
├─────────────────────┼──────┼────────────────────────┤
│ database_schema     │  ✅  │ version 002            │
│ alembic_version     │  ✅  │ 002_fix_outbox         │
│ ffprobe             │  ⚠️  │ not found              │
│ upload_dir          │  ✅  │ ./uploads/ writable    │
│ asr_servers         │  ✅  │ 2/2 online             │
└─────────────────────┴──────┴────────────────────────┘
✅ 系统诊断通过，无阻断性问题
```

状态等级：

| 等级 | 含义 | 需要操作 |
|------|------|---------|
| ✅ ok | 正常 | 无 |
| ⚠️ warning | 功能降级但可运行 | 建议修复 |
| ❌ error | 阻断性问题 | 必须修复 |

常见问题：

- **ffprobe not found**：安装 ffmpeg，用于音频时长估算。缺失时使用文件大小估算，精度降低。
- **schema drift**：运行 `alembic upgrade head` 更新数据库。
- **server offline**：检查 ASR 服务器进程和网络连通性。

---

## 认证

API 使用静态 Token 认证，通过 `X-API-Key` 请求头传递：

| Token | 用户 | 权限 |
|-------|------|------|
| `dev-token-user1` | user1 | 普通用户 |
| `dev-token-user2` | user2 | 普通用户 |
| `dev-token-admin` | admin | 管理员（含节点管理） |

默认关闭认证（开发模式），生产环境需通过配置启用。

CLI 设置认证：

```bash
python -m cli config set api_key dev-token-user1
```

---

## FAQ

**Q: 多文件转写是串行还是并行？**
当你使用 `transcribe` 命令传入多个文件时，所有文件会一次性上传并创建为一个批次，后端调度器会将任务智能分配到所有在线 ASR 服务器并行处理。

**Q: ffprobe 告警可以忽略吗？**
可以。缺少 ffprobe 时，系统使用文件大小估算音频时长，调度精度略降。安装 ffmpeg 后会自动检测。

**Q: 如何查看某个批次的所有任务？**
`python -m cli task list --group <group_id>` 或 `curl http://localhost:8000/api/v1/task-groups/<group_id>/tasks`

**Q: 如何一次性下载所有结果？**
`python -m cli task result --group <group_id> --format txt,srt --output-dir ./results/` 会下载所有格式并生成 `batch-summary.json` 摘要文件。

**Q: 任务创建后处于 PREPROCESSING 状态很久？**
检查是否有 ASR 服务器在线：`python -m cli server list`。如果没有在线节点，任务会排队等待。

**Q: 如何给 AI Agent 或脚本使用 CLI？**
使用 `--output json --quiet` 模式，输出结构化 JSON。例如：
`python -m cli task list --output json --quiet`

**Q: 回调通知怎么配置？**
在创建任务时指定 `--callback URL`（CLI）或 `"callback": {"url": "...", "secret": "..."}` (API)。系统使用 Outbox 模式 + HMAC-SHA256 签名确保可靠投递。

**Q: 批量上传时部分文件失败怎么办？**
CLI 会在输出和 JSON 摘要中列出失败的文件（`upload_failures` 字段），并以非零退出码（exit 1）退出。已成功上传的文件仍会正常转写，不会因为部分失败而全部放弃。

**Q: 从旧版本数据库升级需要注意什么？**
运行 `alembic upgrade head`。迁移 `002_fix_callback_outbox_schema` 会自动为已有记录回填 `outbox_id`（ULID），不会丢失待投递的回调数据。

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (aiosqlite) / PostgreSQL (asyncpg, 可选) + SQLAlchemy 2.0 |
| 任务调度 | 进程内 BackgroundTaskRunner（asyncio）+ LPT 按速度比例分配调度器 |
| ASR 对接 | WebSocket (websockets) |
| 前端 | Vue 3 + Vite + Element Plus + ECharts |
| 监控 | Prometheus + Grafana + Alertmanager |
| CLI | Typer + Rich + httpx |
| 部署 | Docker Compose |

## Docker 部署

```bash
cd 3-dev/src/backend

# 启动全部服务
docker-compose up -d

# 可选：使用 PostgreSQL
# 创建 .env 文件：
#   POSTGRES_PASSWORD=your_secure_password
#   ASR_DATABASE_URL=postgresql+asyncpg://asr:your_secure_password@postgres:5432/asr_tasks
docker compose --profile postgres up -d
docker compose exec web alembic upgrade head
```

服务端口：

| 服务 | 地址 |
|------|------|
| API 文档 | <http://localhost:8000/docs> |
| 前端 | <http://localhost:5173> |
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3001>（admin/admin） |

## 测试

### 测试前环境准备

```bash
cd 3-dev/src/backend

# 先做只读评估，确认当前 backend/data 状态
python ../../../6-skills/reset-asr-db-before-test/scripts/reset_db.py --dry-run

# 需要干净测试环境时执行重置（会自动检测数据库是否被后端占用）
python ../../../6-skills/reset-asr-db-before-test/scripts/reset_db.py

# 重置服务器配置并插入默认测试节点
python ../../../6-skills/reset-asr-db-before-test/scripts/reset_db.py --reset-servers

# CI 环境：跳过备份和确认
python ../../../6-skills/reset-asr-db-before-test/scripts/reset_db.py --no-backup --force
```

详细参数和行为说明见 [6-skills/reset-asr-db-before-test/SKILL.md](6-skills/reset-asr-db-before-test/SKILL.md)。该技能默认针对 `3-dev/src/backend/data/` 工作，不会清理仓库根目录的历史 `data/` 目录。

### 后端测试

```bash
cd 3-dev/src/backend

# 全量测试
python -m pytest "../../../4-tests/scripts/" -v --cov=app

# 单元测试
python -m pytest "../../../4-tests/scripts/unit/" -v

# 集成测试
python -m pytest "../../../4-tests/scripts/integration/" -v

# E2E 测试
python -m pytest "../../../4-tests/scripts/e2e/" -v

# 压力测试
locust -f "../../../4-tests/scripts/load/locustfile.py" --headless -u 50 -r 10 -t 5m
```

### 浏览器 E2E 测试

浏览器端到端测试覆盖真实用户路径：文件上传 → 批量转写 → 任务列表观察 → 结果下载 → 工件归档。提供 4 个测试配置（profile），按覆盖范围递增：

| Profile | 用途 | 文件数 |
|---------|------|--------|
| `smoke` | 日常快速回归 | 3 |
| `remote-standard` | 远端节点 / 受限带宽 | 5 |
| `standard` | 功能合并前验证 | 4-6 |
| `full` | 发布前全量验证 | 全部 |

```bash
cd 3-dev/src/frontend

# 生成测试素材批次
npm run test:e2e:prepare:smoke
npm run test:e2e:prepare:remote-standard

# 执行浏览器 E2E（自动启动前后端）
npm run test:e2e:smoke
npm run test:e2e:remote-standard
```

详细的流程编排、断言策略、跨平台适配和工件归档规范见 [6-skills/funasr-task-manager-web-e2e/SKILL.md](6-skills/funasr-task-manager-web-e2e/SKILL.md)。

## 项目结构

```
funasr-task-manager/
├── 2-design/                          # 设计文档与评审报告
├── 3-dev/src/
│   ├── start.sh / start.ps1          # 一键启停脚本
│   ├── stop.sh  / stop.ps1
│   ├── backend/
│   │   ├── app/                       # FastAPI 应用
│   │   │   ├── api/                   # 路由层（tasks, servers, task_groups, health）
│   │   │   ├── models/                # SQLAlchemy ORM 模型
│   │   │   ├── services/              # 业务逻辑（scheduler, task_runner, diagnostics）
│   │   │   └── storage/               # 仓储层 + 文件管理
│   │   ├── cli/                       # CLI 工具
│   │   │   ├── commands/              # 子命令（transcribe, task, server, system）
│   │   │   ├── api_client.py          # HTTP API 客户端
│   │   │   └── main.py                # 入口 + 全局选项
│   │   └── alembic/                   # 数据库迁移
│   └── frontend/                      # Vue 3 前端 + Playwright E2E
├── 4-tests/scripts/
│   ├── unit/                          # 单元测试
│   ├── integration/                   # 集成测试
│   ├── e2e/                           # 端到端测试（API/CLI 视角）
│   └── load/                          # 压力测试
├── 6-skills/                          # Agent 可复用的自动化技能
│   ├── reset-asr-db-before-test/      # 测试前数据库重置与 dry-run 评估
│   └── funasr-task-manager-web-e2e/   # 浏览器 E2E 测试流程编排与素材管理
├── 7-data/                            # 本地数据（gitignore，不入库）
│   ├── assets/                        # 测试素材（音视频文件）
│   └── outputs/                       # 测试输出工件
└── README.md
```

`6-skills/` 目录存放面向 AI Agent 和开发者的可复用自动化技能。每个技能包含一份 `SKILL.md`（触发条件、操作流程、参数规则）和配套脚本。Agent 在对话中根据 SKILL.md 的 `description` 字段自动匹配并触发对应技能。

## 许可证

内部项目，仅限授权使用。
