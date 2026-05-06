---
name: funasr-task-manager-local-batch-transcribe
description: >
  Scan server-local directories for audio/video files, batch-submit them for
  transcription, monitor progress with proactive feedback, archive results,
  and handle retries. Use when: user requests batch transcription of local files,
  mentions scanning directories, inbox, or wants to retry failed items.
---

# 服务器本地文件批量转写

`funasr-task-manager-local-batch-transcribe` 是面向服务器本地文件的批量转写自动执行规程。它把"发现文件 → 预检查 → 批量提交 → 进度监控 → 结果归档 → 失败重试"固化为 Phase 0-7，智能体拿到触发词即可自主走完整个流程。

## 执行检查清单（强制）

> **实时通知规范**：本 Skill 的所有用户通知必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。禁止用普通文本替代 `send_user_notice()`。

> **强制规则**：Agent 在执行本 Skill 流程时，**必须逐条通过 `send_user_notice()` 确认以下通知已送达**。禁止静默执行整个批量流程。禁止把阶段通知仅写入普通 assistant 文本回复。

| # | 检查项 | 时机 | 模板引用 | `send_user_notice()` 后再执行 |
|---|--------|------|---------|------------------------------|
| 1 | 启动通知 | Phase 1 开始 | `progress-templates.md` §1 | 目录扫描 |
| 2 | 扫描结果通知 | Phase 2 完成 | `progress-templates.md` §2 | 预检查 |
| 3 | 预估耗时通知 | Phase 3 完成 | `progress-templates.md` §2 | 批量提交 |
| 4 | 提交确认 | Phase 4 每 chunk 完成 | `progress-templates.md` §3 | 下一 chunk / 监控 |
| 5 | 定期进度更新 | Phase 5 每 30s 或每 5 个完成 | `progress-templates.md` §4 | 继续轮询 |
| 6 | 异常即时通知 | 任何阶段出错 | `progress-templates.md` §5 | 错误处理 |
| 7 | 完成汇总通知 | Phase 6 完成 | `progress-templates.md` §6 | 归档/退出 |

#### `send_user_notice()` 调用方式

**OpenClaw 环境（首选）：**

```json
{"name": "message", "arguments": {"action": "send", "message": "我将扫描 runtime/agent-local-batch/inbox/ 中的待转写文件..."}}
```

**CLI fallback（无 message tool 时）：**

```bash
python -m cli notify send --text "我将扫描 runtime/agent-local-batch/inbox/ 中的待转写文件..."
```

**时序要求**：每条通知必须在对应耗时操作**之前**发送并等待返回成功，然后再执行扫描/提交/轮询等操作。

> **2026-05-05 排查结论**：批量转写 session `c6105436` 中 Agent 有 `message` tool 可用但未调用，14:14 发出的指令到 14:25 才集中收到所有中间通知。根因是 Agent 只输出普通文本，被 OpenClaw 飞书 bridge 在 turn 结束后统一推送。修复方案：**每个通知点必须显式调用 `send_user_notice()`**。

## 触发条件

### 自动触发

- 用户要求批量转写服务器本地文件
- 用户要求扫描本地目录、待处理目录、inbox 或 workdir
- 用户要求继续上次批量转写
- 用户要求重试失败项

### 关键词

`本地批量转写` / `服务器本地文件` / `扫描目录` / `待处理目录` / `inbox` / `批量识别` / `继续跑` / `重试失败` / `local batch transcribe`

### 不触发

- 用户在聊天中发送单个音视频文件（→ `channel-intake`）
- 用户要求 benchmark 或服务器管理
- 纯文本闲聊

## 核心执行流程

### Phase 0：运行上下文检查

1. 确认当前目录是 `funasr-task-manager` 仓库（存在 `3-dev/src/backend/app/main.py`）。
2. 检查后端健康状态：

```bash
curl -sf http://127.0.0.1:15797/health
```

3. 后端不可达时，先进入 `funasr-task-manager-init` 启动流程。
4. 确认 `runtime/agent-local-batch/` 目录存在；不存在则幂等创建全部子目录。
5. 检查是否有未完成的批次（扫描 `manifests/` 目录中 status 为 `INTERRUPTED`/`SUBMITTING`/`MONITORING` 的文件），如有则提示用户是否恢复。

### Phase 1：确定扫描来源

优先级：

1. 用户本轮明确指定的目录或文件列表。
2. 用户指定的上次批次 ID（恢复模式）。
3. 默认固定扫描目录 `runtime/agent-local-batch/inbox/`。

如果默认扫描目录为空，直接反馈后退出：

```text
未发现待转写文件。请把音视频文件放入 runtime/agent-local-batch/inbox/，或告诉我具体目录。
```

### Phase 2：扫描与建清单

扫描规则：

| 项 | 规则 |
|----|------|
| 递归 | 默认递归扫描子目录 |
| 支持格式 | `.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm` |
| 跳过隐藏文件 | 跳过以 `.` 开头的文件和系统临时文件（`Thumbs.db`、`.DS_Store`） |
| 跳过未完成文件 | 文件大小为 0 或 mtime 在 10 秒内变化时暂不提交 |
| 去重依据 | 文件名 + 文件大小(bytes) + mtime(unix timestamp)，拼接为 `{name}-{size}-{mtime_int}` |
| 已完成跳过 | 同目录下最近 manifest 中标记 `SUCCEEDED` 且指纹未变化时跳过 |

创建批次清单：

```text
runtime/agent-local-batch/manifests/local-YYYYMMDD-HHMMSS.json
```

清单格式详见 `references/manifest-schema.json`。

**此阶段完成后必须向用户反馈扫描结果。**

### Phase 3：媒体预检查

对每个候选文件执行 `funasr-task-manager-media-preflight` 逻辑：

1. 验证文件存在、大小 > 0、扩展名在支持列表中。
2. 使用 `ffprobe` 获取时长、编码、采样率、声道数。
3. 标记是否需要转码（非 wav/pcm 格式）。
4. 累计总音频时长。

预检查失败的文件状态设为 `FAILED_PRECHECK`，写入错误原因，不提交。

**估算总耗时**：

```
estimated_wall_clock = total_audio_duration_sec * rtf / num_online_servers / avg_concurrency
```

RTF 优先从 `GET /api/v1/stats` 获取 `avg_rtf` 字段；无数据时使用默认值 0.1。

**此阶段完成后向用户反馈预估耗时（见 progress-templates §2）。**

### Phase 4：批量提交

#### 设计原则：以 CLI 为执行骨架

优先使用已验证的 CLI 命令，复用其上传重试、文件名映射、task_group_id 追踪能力。

#### 提交命令

工作目录：`3-dev/src/backend`

**小批量（≤50 文件）— 阻塞模式：**

```bash
python -m cli transcribe \
  <file1> <file2> ... \
  --batch \
  --language auto \
  --format txt \
  --output-dir ../../../runtime/agent-local-batch/outputs/<batch_id> \
  --poll-interval 10 \
  --timeout 3600
```

**大批量（>50 文件）— 分 chunk 提交：**

每 chunk 最多 50 个文件：

```bash
python -m cli transcribe \
  <chunk_files...> \
  --batch --no-wait --json-summary \
  --language auto \
  --output-dir ../../../runtime/agent-local-batch/outputs/<batch_id>
```

解析输出的 JSON：

```json
{"task_group_id": "...", "task_ids": [...], "task_count": N}
```

更新 manifest 中对应 items 的 `task_id`、`task_group_id`、`status → SUBMITTED`。

#### 分块策略

| 文件数 | 策略 |
|--------|------|
| 1-50 | 单批提交，CLI 阻塞等待 |
| 51-300 | 每 50 个一批，`--no-wait` 提交后统一监控 |
| 300+ | 同上，但提交前必须向用户确认 |

**每个 chunk 提交后向用户反馈（见 progress-templates §3）。**

#### HTTP API 备选

如果平台无法执行 CLI，可直接调用 API：

1. `POST /api/v1/files/upload` — 逐个上传
2. `POST /api/v1/tasks` — 批量创建（body: `{"items": [...], "segment_level": "10m"}`）
3. 记录返回的 `task_group_id` 和 `task_id` 列表

### Phase 5：进度监控与反馈

**核心要求**：Agent 必须主动反馈，不允许等用户追问。

#### 监控方式选择

| 批量规模 | 推荐方式 |
|----------|---------|
| ≤50 文件（单 group） | Layer 1：CLI 阻塞模式，解析 stdout |
| >50 文件（多 group） | Layer 2：循环 API 轮询，聚合进度 |

#### Layer 2 轮询协议

```
while not all_groups_terminal:
    for each group_id in manifest.chunks:
        response = GET /api/v1/task-groups/{group_id}/tasks?page_size=500
        for task in response.items:
            update manifest item status by task_id
    
    aggregate progress across all chunks
    
    if progress_changed or elapsed_since_last_report > 30s:
        report_to_user(progress)  # 见 progress-templates §4
    
    save manifest to disk
    sleep(poll_interval)  # 默认 15s
```

> **注意**：`GET /api/v1/task-groups/{group_id}` 仅返回聚合统计（progress 百分比），
> 不包含逐任务状态。更新 manifest items 的逐项 status 必须使用
> `GET /api/v1/task-groups/{group_id}/tasks` 端点或 `python -m cli task list --group {gid} --json`。

#### 让步检测（与 channel-intake 协作）

每次轮询时检查 `GET /api/v1/stats`：
- 如果 `queue_depth` 中出现非本 batch 的新任务，进入让步模式
- 暂停提交新 chunk
- 已运行的 batch 任务继续
- 等待 intake 任务全部完成后恢复
- 通知用户（见 progress-templates §7）

#### 超时处理

| 场景 | 阈值 | 动作 |
|------|------|------|
| 单文件超时 | 文件时长 × 2 + 300s | 标记 TIMEOUT |
| 整批超时 | `--timeout` 参数值（默认 3600s） | 标记未完成项为 TIMEOUT |
| 后端无响应 | 连续 3 次轮询失败 | 暂停并通知用户 |

### Phase 6：结果归档

对所有 `SUCCEEDED` 任务下载结果：

```bash
# 单任务结果
python -m cli task result <task_id> --format txt --save <output_path>

# 批量下载整个 task group 的结果（推荐）
python -m cli task result --group <group_id> --format txt --output-dir <batch_output_dir>
```

归档目录结构：

```text
runtime/agent-local-batch/outputs/<batch_id>/
├── txt/           # 转写文本（默认）
├── json/          # 结构化结果（用户要求时）
├── srt/           # 字幕文件（用户要求时）
└── summary-<batch_id>-YYYYMMDD.md
```

结果文件命名规则：`{原始文件名去扩展名}.txt`

批次汇总文件必须满足项目规范（文件名以 `-YYYYMMDD.md` 结尾）。

**此阶段完成后向用户反馈完成汇总（见 progress-templates §6）。**

### Phase 7：失败处理与重试

失败分类及处理：

| 状态 | 可自动重试 | 处理方式 |
|------|-----------|---------|
| `FAILED_PRECHECK` | 否 | 提示用户检查文件格式 |
| `FAILED_UPLOAD` | 是（1 次） | 自动重试上传 |
| `FAILED_TRANSCRIBE` | 需确认 | 询问用户是否重试 |
| `FAILED_DOWNLOAD` | 是（1 次） | 自动重试下载 |
| `TIMEOUT` | 需确认 | 查询后端状态后决定 |

用户说"重试失败项"时：

1. 读取最近一个 manifest
2. 筛选可重试状态的 items
3. 创建新批次（ID 格式：`retry-of-<原batch_id>-YYYYMMDD-HHMMSS`）
4. 重新进入 Phase 4

**不得覆盖原 manifest。**

#### 断点续传

当检测到 `INTERRUPTED` 状态的 manifest 时：

| 中断点 | 恢复动作 |
|--------|----------|
| 提交阶段 | 从 `resume_from.last_submitted_index` 继续提交 |
| 监控阶段 | 查询所有已提交 group 的最新状态，继续监控 |
| 下载阶段 | 补下载 `output_path` 为空的 SUCCEEDED 任务 |

## 与其他 Skill 的协作

| 协作场景 | Skill | 时机 | 交接数据 |
|---------|-------|------|---------|
| 文件预检查 | `funasr-task-manager-media-preflight` | Phase 3 | 文件路径 → duration/format/warnings |
| 结果格式化 | `funasr-task-manager-result-delivery` | Phase 6 | 可复用其 txt/srt/json 格式化规则 |
| 服务器性能 | `funasr-task-manager-server-benchmark` | Phase 3 | RTF baseline 用于耗时预估 |
| 渠道入口 | `funasr-task-manager-channel-intake` | Phase 5 | 让步检测 — intake 优先 |
| 环境初始化 | `funasr-task-manager-init` | Phase 0 | 后端不可达时触发 |

## 错误处理规范

| 错误场景 | Agent 应做的事 | 不应做的事 |
|---------|--------------|----------|
| 目录不存在 | 报告路径无效，询问正确路径 | 静默创建用户指定的外部目录 |
| 后端不可达 | 尝试启动后端，失败则通知用户 | 静默等待或反复重试 |
| 全部文件预检查失败 | 报告失败原因列表，建议操作 | 尝试强行提交 |
| CLI 命令执行失败 | 报告错误输出，建议排查 | 静默切换到 API 模式 |
| manifest 写入失败 | 报告权限问题 | 继续执行但不记录状态 |
| 后端返回 413 | 报告文件过大，告知大小限制 | 尝试压缩音频 |

## 安全与边界规则

1. 默认不删除、不移动用户源文件。
2. 不把大音视频文件复制到 Git 管理目录。
3. 不把转写全文刷屏输出；长结果保存为文件并给出路径。
4. 不在项目根目录生成散落文件。
5. 失败重试必须创建新批次，不覆盖历史 manifest。
6. manifest 只记录路径、状态、错误信息；敏感文件名按用户要求脱敏。

## 相关文件

- `references/manifest-schema.json`：manifest JSON Schema 定义
- `references/progress-templates.md`：用户交互通知模板
- `references/platform-adapters.md`：各平台进度监控实现差异
