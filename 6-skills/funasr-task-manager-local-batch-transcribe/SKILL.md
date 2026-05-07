---
name: funasr-task-manager-local-batch-transcribe
description: >
  Scan server-local directories for audio/video files, batch-submit them for
  transcription, monitor progress with proactive feedback, archive results,
  and handle retries. Use when: user requests batch transcription of local files,
  mentions scanning directories, inbox, or wants to retry failed items.
---

# 服务器本地文件批量转写

`funasr-task-manager-local-batch-transcribe` 是面向服务器本地文件的批量转写自动执行规程。它把"发现文件 → 预检查 → 批量提交 → 委托监控 → 结果归档 → 失败重试"固化为 Phase 0-7，智能体拿到触发词即可自主走完整个流程。

> **异步调度架构**：本 Skill 采用"主 Agent 调度 + 子 Agent 监控播报"模式。主 Agent 负责 Phase 0-4（扫描/预检/提交），完成提交后立即委托子 Agent 执行 `batch-monitor` Skill 进行进度监控和结果下载（Phase 5-6），自身释放以继续接收新任务。详见 `2-design/主Agent异步调度与子Agent监控播报架构设计-20260506.md`。

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

### Phase 4：批量提交（使用 task-group 短命令）

#### 设计原则

使用 `task-group` 短命令序列，每条命令秒级返回，主 Agent 在每步之间保持控制权，可以调用 `send_user_notice()` 发送通知。

#### Step 4a：扫描（如果 Phase 2 未使用 CLI）

工作目录：`3-dev/src/backend`

```bash
python -m cli --output json task-group scan {source_dir} --chunk-size 50
```

返回 JSON 包含 `items`（文件清单）和 `chunks`（分块信息）。如果 Phase 2 已手动扫描并创建了 manifest，可跳过此步，直接使用已有 manifest。

将扫描结果保存为 manifest 文件：

```bash
python -m cli --output json task-group scan {source_dir} --chunk-size 50 > runtime/agent-local-batch/manifests/{batch_id}.json
```

#### Step 4b：提交

```bash
python -m cli --output json task-group submit \
  --manifest runtime/agent-local-batch/manifests/{batch_id}.json \
  --language auto \
  --segment-level 10m
```

返回 JSON 包含每个 chunk 的 `task_group_id`。

**分 chunk 提交**（大批量场景）：

```bash
python -m cli --output json task-group submit \
  --manifest runtime/agent-local-batch/manifests/{batch_id}.json \
  --chunk 0
```

逐个 chunk 提交，每个 chunk 提交后通过 `send_user_notice()` 反馈（见 progress-templates §3）。

#### 分块策略

| 文件数 | 策略 |
|--------|------|
| 1-50 | 单次 submit 全部提交 |
| 51-300 | 逐 chunk submit，每次通知 |
| 300+ | 同上，但提交前必须向用户确认 |

#### Step 4c：记录 task_group_ids

从 submit 返回的 JSON 中提取所有 `task_group_id`，传递给下一步。

### Phase 5：委托子 Agent 监控（异步调度核心）

> **关键变更**：主 Agent 在此阶段**不再自己轮询**，而是委托子 Agent 执行 `batch-monitor` Skill。

#### 委托协议

主 Agent 发送以下委托消息启动子 Agent：

```
请执行 batch-monitor 监控任务：
- task_group_ids: ["{group_id_1}", "{group_id_2}", ...]
- batch_id: {batch_id}
- total_files: {total_files}
- poll_interval_sec: 30
- timeout_sec: 3600
- result_format: txt
- output_dir: runtime/agent-local-batch/outputs/{batch_id}
```

#### 委托后主 Agent 的行为

1. **发送委托通知**：通过 `send_user_notice()` 告知用户"已启动后台监控"。
2. **释放控制权**：主 Agent 不再阻塞等待批量任务完成。
3. **继续接新任务**：主 Agent 可以响应群聊中新的用户消息和任务请求。

#### 子 Agent 负责的工作

子 Agent 按 `funasr-task-manager-batch-monitor` Skill 执行：

1. 定期查询 `task-group status`
2. 通过 `send_user_notice()` 发送进度通知
3. 全部完成后执行 `task-group download` 下载结果
4. 发送完成汇总通知
5. 退出

#### Fallback：无子 Agent 能力时

如果当前平台不支持子 Agent 并发（如 Codex 沙箱、纯本地 Cursor），主 Agent 回退到**自行轮询模式**：

```
while not all_groups_complete:
    for each group_id:
        python -m cli --output json task-group status {group_id}
    
    if progress_changed or elapsed > 30s:
        send_user_notice(进度通知)
    
    sleep 30
```

回退判断条件：运行环境为 Cursor / Claude Code / Codex 时使用 fallback。

### Phase 6：结果归档（由子 Agent 或主 Agent 完成）

在异步模式下，结果下载由子 Agent 通过 `task-group download` 完成：

```bash
python -m cli --output json task-group download {group_id} \
  --format txt \
  --output-dir runtime/agent-local-batch/outputs/{batch_id}
```

在 fallback 模式下，主 Agent 自行执行上述命令。

归档目录结构：

```text
runtime/agent-local-batch/outputs/<batch_id>/
├── {file1}_result.txt
├── {file2}_result.txt
├── ...
└── batch-summary.json        # 自动生成
```

批次汇总文件必须满足项目规范（文件名以 `-YYYYMMDD.md` 结尾）。

**此阶段完成后向用户反馈完成汇总（见 progress-templates §6 或 monitor-templates §5）。**

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
| **进度监控** | **`funasr-task-manager-batch-monitor`** | **Phase 5** | **task_group_ids + batch_id → 子 Agent 异步播报** |
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
- `references/progress-templates.md`：用户交互通知模板（Phase 0-4 由主 Agent 使用）
- `references/platform-adapters.md`：各平台进度监控实现差异
- `6-skills/funasr-task-manager-batch-monitor/SKILL.md`：子 Agent 监控播报规程（Phase 5-6）
- `6-skills/funasr-task-manager-batch-monitor/references/monitor-templates.md`：子 Agent 通知模板
