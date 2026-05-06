# FunASR Task Manager

集中式离线语音识别（ASR）任务管理系统。对接 FunASR 服务器集群，提供文件上传、智能调度、实时进度追踪和多格式结果下载。

**三种使用方式**：CLI 命令行 · REST API · Web UI

**核心能力**：

- **多服务器智能调度** — LPT + 最早完工时间预规划，空闲节点自动工作窃取
- **长音频 VAD 分段并行** — 超过阈值的音频自动切分，分发到多台服务器并行转写，结果按时间戳合并
- **AI Agent 原生支持** — 8 个配套 Skills，让 AI Agent（OpenClaw / Hermes / Cursor 等）开箱即用地完成聊天渠道转写和服务器本地批量转写全流程

## 你想做什么？

```
├── 人类用户
│   ├── 转写 1 个文件            → python -m cli transcribe audio.mp4
│   ├── 转写多个文件（自动并行）  → python -m cli transcribe *.wav --format txt
│   ├── 只提交不等待              → python -m cli transcribe files --no-wait
│   ├── 查看批次进度              → python -m cli task list --group <group_id>
│   ├── 下载批次结果              → python -m cli task result --group <group_id> --format txt,srt
│   ├── 管理 ASR 服务器           → python -m cli server list / probe / benchmark
│   ├── 发送渠道实时通知          → python -m cli notify send --text "..."
│   ├── 排查系统问题              → python -m cli doctor
│   └── API 集成开发              → 阅读下方 API 参考
│
└── AI Agent
    ├── 环境初始化               → init Skill（自动检测、安装、配置）
    ├── 用户发送音视频 → 自动转写  → channel-intake + result-delivery Skills
    ├── 安装 Skills 到 Agent 平台  → init Phase 6
    └── 详见下方「Agent Skills 体系」
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
uvicorn app.main:app --host 0.0.0.0 --port 15797 --reload
```

> **工作目录说明**
> 后端默认把数据库、上传结果和临时文件写到仓库根目录的 `runtime/storage/`，日志与 PID 文件写到 `runtime/logs/`。这些默认路径已经与当前工作目录解耦，不会再因为从不同目录启动而把持久化数据写进 `3-dev/src/` 或仓库根目录的历史 `data/` 目录。

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

# 默认会把 CLI 下载副本写到仓库根目录 runtime/storage/downloads/

# 指定输出目录
python -m cli transcribe meeting.wav --format srt --output-dir ./runtime/storage/downloads/manual/
```

---

## Agent Skills 体系

本项目为 AI Agent 提供了 **8 个配套 Skills**，安装到 Agent 平台（OpenClaw / Hermes / Cursor 等）后，Agent 可自主完成从环境检测到转写交付的全流程。

Agent 体系有两条主要入口：

- **聊天渠道实时转写**：用户在飞书/企微/Slack 等渠道发送音视频文件，Agent 自动识别意图、下载文件、预检查、提交后端转写、监控状态，并以文件附件回复结果。
- **服务器本地批量转写**：用户要求扫描服务器本地目录或 inbox，Agent 自动建清单、批量提交、持续播报进度、归档结果并处理失败重试。

所有用户可见的阶段进度都必须走统一实时通知原语 `send_user_notice()`：OpenClaw/Hermes 环境优先调用平台 `message` tool；没有可用 `message` tool 时才 fallback 到 CLI `python -m cli notify ...`。普通 assistant 文本只用于最终总结或纯本地终端场景，不能当作聊天渠道实时进度通知。

### Skill 一览

| Skill | 职责 | 触发时机 |
|-------|------|---------|
| **init** | 环境检测、依赖安装、后端启动、Skill 安装、渠道凭据配置、systemd 服务注册 | 首次部署或环境异常时 |
| **channel-intake** | 意图识别、渠道文件下载（飞书/企微/Slack）、预检查、任务提交 | 用户发送音视频文件时 |
| **local-batch-transcribe** | 扫描服务器本地目录、生成批量清单、分 chunk 提交、进度监控、结果归档、失败重试 | 用户要求批量转写本地目录/inbox 时 |
| **media-preflight** | ffprobe 格式验证、时长检测、转码评估 | 文件上传到后端前 |
| **result-delivery** | 任务状态轮询、结果拉取、质量初筛、格式化通知、文件附件发送 | 转写完成时 |
| **server-benchmark** | ASR 节点性能校准（单线程 RTF + 梯度并发吞吐量） | 节点注册或定期校准时 |
| **reset-test-db** | 测试环境数据库重置与 dry-run 评估 | 测试前 |
| **web-e2e** | 浏览器 E2E 测试流程编排与素材管理 | 发版验证时 |

### 协作链路

入口 A：聊天渠道实时转写

```
用户发送音视频
      ↓
  channel-intake          ← 意图识别 + 渠道文件下载 + 任务提交
      ↓
  media-preflight         ← ffprobe 预检 + 转码决策
      ↓
  [后端自动调度转写]       ← VAD 分段 + 多服务器并行
      ↓
  result-delivery         ← 状态轮询 + 质量初筛 + 文件附件回复
```

入口 B：服务器本地批量转写

```
用户要求扫描目录/inbox
      ↓
  local-batch-transcribe  ← 扫描目录 + manifest + 批量预检
      ↓
  CLI --batch 批量提交
      ↓
  local-batch-transcribe  ← 进度监控 + 主动通知 + 结果归档 + 失败重试
      ↓
  result-delivery 格式规范 ← 复用结果摘要和附件交付模板
```

协作规则：`init` 是所有入口的前置环境准备；`media-preflight` 可被两条入口复用；`result-delivery` 是任务创建后的结果出口；`server-benchmark`、`reset-test-db`、`web-e2e` 分别服务性能校准、测试环境和浏览器验证。聊天渠道任务与本地批量任务并发时，聊天渠道入口优先，本地批量入口暂停新提交并等待恢复。

### 实时通知原语

所有 Skill 中出现“通知用户 / 汇报 / 反馈 / 进度更新”的位置，都必须遵循 [渠道实时通知规范](6-skills/_shared/CHANNEL-NOTIFICATION.md)。

适配优先级：

1. 平台原生 `message` tool（OpenClaw / Hermes）：`{"action": "send", "message": "...", "filePath": "..."}`
2. CLI fallback：`python -m cli notify send --text "..."` 或 `python -m cli notify send-file --file result.txt`
3. 普通 assistant 文本：仅限用户能直接看到实时输出的纯本地终端场景

`notify` CLI 当前支持飞书/Lark 文本通知、话题回复、文件附件、token 缓存、soft-fail / `--strict` 模式。详见下方 [notify — 渠道实时通知（飞书）](#notify--渠道实时通知飞书)。

### 安装 Skills 到 Agent 平台

运行 `init` Skill 的 Phase 6 即可一键安装所有 8 个 Skills 和共享工作流文件到目标平台：

- **OpenClaw**：`~/.openclaw/workspace-{name}/skills/`
- **Hermes**：`~/.hermes/skills/`
- **Cursor**：`{project}/.cursor/skills/` 或 `~/.cursor/skills-cursor/`

安装后 Agent 启动时自动加载 ASR 转写能力，无需用户手动指挥。若需要聊天渠道实时进度，还应按 `init` Phase 7 配置渠道凭据与 `notify` fallback。详见 [init Skill](6-skills/funasr-task-manager-init/SKILL.md) 和 [渠道凭据配置](6-skills/funasr-task-manager-init/references/channel-credentials.md)。

### 端口与服务

| 服务 | 默认端口 | 说明 |
|------|---------|------|
| 后端 API | **15797** | Agent 和 CLI 通过此端口调用转写 API |
| 前端 Web UI | **15798** | 人类操作员使用，Agent 不依赖 |
| 预留 | **15799** | 未来扩展 |

后端推荐注册为 **systemd 用户级服务**（`systemctl --user`），无需 sudo，Agent 可直接管理服务生命周期。详见 [init Skill Phase 8](6-skills/funasr-task-manager-init/references/systemd-setup.md)。

---

## 批量转写指南

### CLI 批量模式

多文件时自动启用批量模式，一次性上传并创建所有任务，后端并行调度到多台 ASR 服务器：

```bash
# 转写目录下所有 wav 文件（默认下载到 runtime/storage/downloads/）
python -m cli transcribe *.wav --format txt

# 转写指定文件
python -m cli transcribe ep01.mp4 ep02.mp4 ep03.mp4 --format srt --output-dir ./runtime/storage/downloads/srt/

# 强制批量模式（即使只有 1 个文件）
python -m cli transcribe single.wav --batch --format json

# 异步提交，不等待完成
python -m cli transcribe *.mp3 --no-wait --json-summary

# 完成后下载多种格式结果 + 生成摘要
python -m cli task result --group <group_id> --format txt,json,srt
```

说明：服务端原始持久化始终写入仓库根目录 `runtime/storage/uploads/` 和 `runtime/storage/results/`；CLI 的 `--output-dir` 只是额外下载一份副本，默认放到 `runtime/storage/downloads/`，避免误写到 `3-dev/src/backend/` 当前工作目录。

公开 benchmark 样本统一放在 `3-dev/benchmark/samples/`（已纳入版本控制），当前固定样本为 `test.mp4` 和 `tv-report-1.wav`。

批量下载后的输出目录结构：

```
runtime/storage/downloads/
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
curl -X POST http://localhost:15797/api/v1/files/upload -F "file=@ep01.wav"
# → {"file_id": "01JQXXX1..."}

curl -X POST http://localhost:15797/api/v1/files/upload -F "file=@ep02.wav"
# → {"file_id": "01JQXXX2..."}

# 2. 批量创建任务
curl -X POST http://localhost:15797/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"file_id": "01JQXXX1...", "language": "zh"},
      {"file_id": "01JQXXX2...", "language": "zh"}
    ]
  }'

# 3. 查看批次进度
curl http://localhost:15797/api/v1/task-groups/<group_id>

# 4. 下载结果（zip 打包）
curl -o results.zip http://localhost:15797/api/v1/task-groups/<group_id>/results?format=zip
```

### 前端批量上传

Web UI 支持拖拽多文件上传和批量任务管理。访问 `http://localhost:15798` 打开前端界面。

---

## CLI 命令参考

所有命令的工作目录为 `3-dev/src/backend`，命令前缀为 `python -m cli`。

### 全局选项

| 选项 | 简写 | 环境变量 | 说明 | 默认值 |
|------|------|---------|------|--------|
| `--server` | `-s` | `ASR_API_SERVER` | API 服务地址 | `http://localhost:15797` |
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

# 批量（自动并行调度到多台服务器，默认下载到 runtime/storage/downloads/）
python -m cli transcribe *.wav --format txt

# 异步提交
python -m cli transcribe *.mp3 --no-wait --json-summary
```

| 选项 | 说明 |
|------|------|
| `--language, -l` | 识别语言，默认 `auto` |
| `--hotwords` | 热词列表，逗号分隔 |
| `--format, -f` | 结果格式: `json`/`txt`/`srt` |
| `--output-dir, -d` | 下载副本目录，默认 `runtime/storage/downloads/` |
| `--save` | 单文件时保存到指定路径 |
| `--no-wait` | 只提交不等待 |
| `--batch` | 强制批量模式 |
| `--download/--no-download` | 完成后是否下载 |
| `--json-summary` | 输出 JSON 格式摘要 |
| `--callback` | 完成后回调地址 |
| `--poll-interval` | 轮询间隔(秒)，默认 5 |
| `--timeout` | 等待超时(秒)，默认 3600 |
| `--segment-level` | 切分策略：`off`（关闭切分）/ `10m`（默认，按时长阈值自动决定）/ `20m` / `30m` |

### task — 任务管理

```bash
# 创建任务
python -m cli task create <file_id1> <file_id2> --language zh

# 创建任务（指定切分策略）
python -m cli task create <file_id> --segment-level 20m

# 查看任务列表
python -m cli task list
python -m cli task list --status SUCCEEDED --page 1
python -m cli task list --group <group_id>    # 按批次筛选

# 查看任务详情
python -m cli task info <task_id>

# 下载单个任务结果
python -m cli task result <task_id> --format srt --save output.srt

# 下载整批结果（支持多格式同时导出，默认下载到 runtime/storage/downloads/）
python -m cli task result --group <group_id> --format txt,json,srt

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

# 注册并立即执行 benchmark（自动校准 RTF 基线和推荐并发，终端实时显示进度）
python -m cli server register --id asr-01 --host 192.168.1.100 --port 10095 --protocol v2_new --benchmark

# 探测节点连通性和能力
python -m cli server probe asr-01

# 单节点 benchmark（单线程 RTF + 梯度并发吞吐量测试，自动校准 max_concurrency）
python -m cli server benchmark asr-01

# 全量 benchmark（对所有在线节点执行，刷新 RTF 基线与吞吐量指标）
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
| `connect_only` | 仅检查 WebSocket / 网络连通性，不做性能测试 |
| `offline_light` | 连通性 + 离线转写能力探测（默认） |
| `twopass_full` | 完整双通道探测 |

`probe` 只做连通性与能力探测，不参与 benchmark，也不会更新任何 RTF 基线。

### notify — 渠道实时通知（飞书）

`notify` 用于 Agent 在没有平台原生 `message` tool 时，通过飞书 API 发送实时进度通知和结果附件。默认 soft-fail：通知失败只输出 warning，不中断主流程；加 `--strict` 后失败返回 exit 1。

```bash
# 检查飞书凭据
python -m cli notify auth-check

# 发送文本
python -m cli notify send --text "⏳ 正在从飞书下载文件..."

# 从文件或 stdin 发送多行文本
python -m cli notify send --text-file /tmp/notice.txt
echo "批量转写进度：35/50 已完成" | python -m cli notify send --stdin

# 发送结果附件
python -m cli notify send-file --file /tmp/result.txt --filename "会议记录.txt"

# 回复到指定飞书消息线程
python -m cli notify send --text "处理中..." --reply-to om_xxxxxxxx
```

配置来源优先级：环境变量 > CLI 配置文件。

| 配置 | 环境变量 | CLI 配置键 |
|------|----------|------------|
| 飞书 App ID | `FEISHU_APP_ID` | `notify.feishu_app_id` |
| 飞书 App Secret | `FEISHU_APP_SECRET` | `notify.feishu_app_secret` |
| 默认群聊 ID | `FEISHU_CHAT_ID` | `notify.default_chat_id` |
| 默认回复消息 ID | `FEISHU_REPLY_TO` | `notify.default_reply_to` |

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
python -m cli config set server http://asr-server:15797

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
| POST | `/api/v1/servers/{server_id}/probe` | 探测单节点连通性与能力，不执行 benchmark |
| POST | `/api/v1/servers/{server_id}/benchmark` | 对单节点执行真实 benchmark |
| POST | `/api/v1/servers/benchmark` | 对所有在线节点执行真实 benchmark 并刷新 RTF 基线 |
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
curl -X POST http://localhost:15797/api/v1/files/upload \
  -F "file=@recording.wav"

# 创建单个任务
curl -X POST http://localhost:15797/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"items": [{"file_id": "01JQXXX...", "language": "zh"}]}'

# 创建批量任务（含回调）
curl -X POST http://localhost:15797/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"file_id": "FILE_ID_1", "language": "zh"},
      {"file_id": "FILE_ID_2", "language": "zh", "options": {"hotwords": "FunASR,转写"}}
    ],
    "callback": {"url": "https://your-server.com/webhook", "secret": "your-hmac-key"}
  }'

# 创建任务（控制 VAD 切分行为）
curl -X POST http://localhost:15797/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "items": [{"file_id": "FILE_ID", "language": "zh"}],
    "segment_level": "20m"
  }'

# 查看批次状态
curl http://localhost:15797/api/v1/task-groups/<group_id>
# → {"task_group_id": "...", "total": 5, "succeeded": 3, "failed": 0, ...}

# 下载转写结果（txt 格式）
curl http://localhost:15797/api/v1/tasks/<task_id>/result?format=txt

# 下载批次 zip
curl -o results.zip http://localhost:15797/api/v1/task-groups/<group_id>/results?format=zip

# 探测节点
curl -X POST http://localhost:15797/api/v1/servers/asr-01/probe?level=offline_light

# 对单节点执行真实 benchmark（仅使用公开样本 test.mp4 + tv-report-1.wav）
curl -X POST http://localhost:15797/api/v1/servers/asr-01/benchmark

# 系统诊断
curl http://localhost:15797/api/v1/diagnostics
```

---

## 多服务器配置

### 工作原理

ASR 任务管理器支持注册多台 FunASR 服务器，使用 LPT（最长处理时间优先）+ 最早完工时间做首轮预规划，并按槽位队列持续补位；当快节点先清空自己的队列时，会从其他节点队列尾部工作窃取更适合的短任务，以缩短整批完工时间。

### 配置流程

```bash
# 1. 注册节点（推荐加 --benchmark，一步完成注册 + 性能基准测试）
python -m cli server register --id asr-gpu-01 --host 10.0.0.1 --port 10095 --protocol v2_new --benchmark
python -m cli server register --id asr-gpu-02 --host 10.0.0.2 --port 10095 --protocol v2_new --benchmark

# 也可以先注册再单独测速
python -m cli server register --id asr-gpu-03 --host 10.0.0.3 --port 10095 --protocol v2_new
python -m cli server benchmark asr-gpu-03

# 2. 探测连通性和能力（可选，benchmark 已包含连通性检查）
python -m cli server probe asr-gpu-01

# 3. 全量 benchmark（刷新所有在线节点的 RTF 基线与吞吐量指标）
python -m cli server benchmark

# 4. 查看节点状态
python -m cli server list
```

### 调度原理

| 概念 | 说明 |
|------|------|
| **RTF**（Real-Time Factor） | 处理1秒音频需要的时间。RTF=0.1 表示10x加速 |
| **双指标 Benchmark** | `single_rtf`（单线程延迟）+ `throughput_rtf`（并发满载吞吐），前者用于 ETA 估算，后者驱动配额分配 |
| **梯度并发 + 退化检测** | 逐级测试 N∈[1,2,4,8] 并发，自动识别吞吐退化点并校准 `max_concurrency` |
| **LPT 调度** | 优先将最长文件分配给最快的服务器 |
| **最早完工时间** | 考虑当前队列负载，选择预估最早完成的节点 |
| **配额分配** | 按 `max_concurrency / base_rtf` 速度比例在全局批次内分配任务配额，并发多且单线程快的节点获得更多任务 |
| **槽位队列预规划** | 首轮为每个虚拟并发槽位生成有序任务队列，补位时直接从本槽位取下一个 |
| **工作窃取** | 快节点空闲且自身队列清空时，可从其他节点队列尾部窃取更快完成的短任务 |
| **VAD 分段并行** | 超过阈值（target × 1.2，默认 12 分钟）的长音频自动 VAD 切分为多个段，采用双向交替搜索最佳切点，并行分发到多台服务器，结果按时间戳合并。支持 10m/20m/30m 三级切分力度，搜索步长按档位比例设定 |
| **自动重试** | 瞬时故障（网络超时、协议错误）的任务自动从 FAILED 回到 QUEUED 重新调度，回调和并发槽位仅在真正终态时释放 |
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
│ database_schema     │  ✅  │ version 004            │
│ alembic_version     │  ✅  │ 004_task_segments      │
│ ffprobe             │  ⚠️  │ not found              │
│ upload_dir          │  ✅  │ runtime/storage/uploads writable │
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

**Q: benchmark 用的是什么样本？**
当前统一使用 `3-dev/benchmark/samples/` 下的 `test.mp4` 和 `tv-report-1.wav`（已纳入版本控制，克隆即可用）。`probe` 仅做 WebSocket 连通性探测，不发送真实音频、不计算 RTF；`benchmark` 使用上述两个真实样本执行端到端转写来测量 RTF。

**Q: 如何查看某个批次的所有任务？**
`python -m cli task list --group <group_id>` 或 `curl http://localhost:15797/api/v1/task-groups/<group_id>/tasks`

**Q: 如何一次性下载所有结果？**
`python -m cli task result --group <group_id> --format txt,srt` 会把所有格式下载到 `runtime/storage/downloads/` 并生成 `batch-summary.json` 摘要文件；如果你只想导出副本到别处，再显式指定 `--output-dir`。

**Q: 任务显示 FAILED 但随后又变成 SUCCEEDED 了？**
系统内置自动重试机制。任务首次失败（如网络抖动）后会自动从 FAILED 重新排队，在未达到最大重试次数前属于可恢复状态。API 响应中的 `is_terminal` 字段标识任务是否已到达真正终态——SUCCEEDED 和 CANCELED 总是终态，FAILED 仅在重试耗尽后才是终态。SSE 流和 CLI 批量轮询都依赖此字段判断何时停止观察。

**Q: 长音频（10 分钟以上）是怎么处理的？**
系统内置 VAD 分段并行转写。默认当音频超过 12 分钟（target × 1.2 触发阈值）时，自动执行 canonical WAV 转换 → VAD 静音检测 → 双向交替搜索最佳切点 → 物理切段 → 并行分发到多台服务器 → 结果按时间戳合并。用户侧看到的仍然是一个任务，段级调度和合并完全透明。可通过 `--segment-level=off` 关闭切分，或用 `--segment-level=20m/30m` 调整切分粒度（减少切分次数）。

**Q: `segment_level` 的各个级别有什么区别？**
`segment_level` 合并了原来的 `auto_segment` 和 `segment_level` 两个参数，取值为 `off`/`10m`/`20m`/`30m`。`off` 关闭切分，整文件直接转写。`10m`（默认）以 10 分钟为目标切分，双向搜索窗口 9~12 分钟，<12 分钟不拆；`20m` 以 20 分钟为目标，双向搜索窗口 18~24 分钟，<24 分钟不拆；`30m` 以 30 分钟为目标，双向搜索窗口 27~36 分钟，<36 分钟不拆。搜索采用"后→前→后"双向交替策略，搜索步长按档位比例设定（60s/120s/180s）。级别越大，切分次数越少，边界质量损失越小，但单段处理时间更长。

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
运行 `alembic upgrade head`。迁移 `002_fix_callback_outbox_schema` 会自动为已有记录回填 `outbox_id`（ULID）；`003_add_throughput_rtf` 会为 `server_instances` 表新增 `throughput_rtf` 和 `benchmark_concurrency` 列（nullable），已有服务器首次 benchmark 后自动填充；`004_create_task_segments` 会创建 `task_segments` 表，用于 VAD 分段并行转写的段级调度和状态跟踪。

---

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (aiosqlite) / PostgreSQL (asyncpg, 可选) + SQLAlchemy 2.0 |
| 任务调度 | 进程内 BackgroundTaskRunner（asyncio）+ LPT/EFT + 槽位队列预规划 + 工作窃取 + RTF 运行时校准 |
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
| API 文档 | <http://localhost:15797/docs> |
| 前端 | <http://localhost:15798> |
| Prometheus | <http://localhost:9090> |
| Grafana | <http://localhost:3001>（admin/admin） |

## 测试

### 测试前环境准备

```bash
cd 3-dev/src/backend

# 先做只读评估，确认当前 runtime/storage 状态
python ../../../6-skills/funasr-task-manager-reset-test-db/scripts/reset_db.py --dry-run

# 需要干净测试环境时执行重置（会自动检测数据库是否被后端占用）
python ../../../6-skills/funasr-task-manager-reset-test-db/scripts/reset_db.py

# 重置服务器配置并插入默认测试节点
python ../../../6-skills/funasr-task-manager-reset-test-db/scripts/reset_db.py --reset-servers

# CI 环境：跳过备份和确认
python ../../../6-skills/funasr-task-manager-reset-test-db/scripts/reset_db.py --no-backup --force
```

详细参数和行为说明见 [6-skills/funasr-task-manager-reset-test-db/SKILL.md](6-skills/funasr-task-manager-reset-test-db/SKILL.md)。该技能默认针对 `runtime/storage/` 工作，不会扫描旧的 `3-dev/src/backend/data/` 或仓库根目录 `data/` 目录。

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
│   ├── benchmark/                    # 公开 benchmark 样本（纳入版本控制）
│   │   └── samples/                  # test.mp4 + tv-report-1.wav
│   ├── start.sh / start.ps1          # 一键启停脚本
│   ├── stop.sh  / stop.ps1
│   ├── backend/
│   │   ├── app/                       # FastAPI 应用
│   │   │   ├── api/                   # 路由层（tasks, servers, task_groups, health）
│   │   │   ├── models/                # SQLAlchemy ORM 模型
│   │   │   ├── services/              # 业务逻辑（scheduler, task_runner, diagnostics）
│   │   │   └── storage/               # 仓储层 + 文件管理
│   │   ├── cli/                       # CLI 工具
│   │   │   ├── commands/              # 子命令（transcribe, task, server, notify, system）
│   │   │   ├── api_client.py          # HTTP API 客户端
│   │   │   └── main.py                # 入口 + 全局选项
│   │   └── alembic/                   # 数据库迁移
│   └── frontend/                      # Vue 3 前端 + Playwright E2E
├── 4-tests/
│   ├── batch-testing/                 # 测试素材与批量测试工件（gitignore）
│   │   ├── assets/                    # 音视频测试素材
│   │   └── outputs/                   # cli / e2e / benchmark 输出
│   └── scripts/
│       ├── unit/                      # 单元测试
│       ├── integration/               # 集成测试
│       ├── e2e/                       # 端到端测试（API/CLI 视角）
│       └── load/                      # 压力测试
├── 6-skills/                          # Agent 可复用的自动化技能
│   ├── _shared/                                # 共享 ASR 工作流与实时通知规范
│   ├── funasr-task-manager-init/               # 环境初始化与启动（Python/Docker）
│   ├── funasr-task-manager-channel-intake/     # 音频入口与意图编排（飞书/企微/Slack）
│   ├── funasr-task-manager-local-batch-transcribe/ # 服务器本地文件批量转写
│   ├── funasr-task-manager-media-preflight/    # 媒体文件预检查（格式/时长/转码评估）
│   ├── funasr-task-manager-result-delivery/    # 结果交付与质量初筛
│   ├── funasr-task-manager-server-benchmark/   # 服务器性能校准（RTF 基线/并发梯度）
│   ├── funasr-task-manager-reset-test-db/      # 测试前数据库重置与 dry-run 评估
│   └── funasr-task-manager-web-e2e/            # 浏览器 E2E 测试流程编排与素材管理
├── runtime/                           # 运行时目录（gitignore）
│   ├── storage/                       # 数据库 / 上传 / 结果 / 临时文件
│   └── logs/                          # 后端/前端日志、PID 与迁移日志
└── README.md
```

每个 Skill 包含 `SKILL.md`（触发条件、执行流程、参数规则）和配套脚本/参考文档。完整介绍见上方 [Agent Skills 体系](#agent-skills-体系)。

## 许可证

内部项目，仅限授权使用。
