---
name: funasr-task-manager-local-batch-transcribe
description: >
  Scan server-local directories for audio/video files, batch-submit them for
  transcription, monitor progress with proactive feedback, archive results,
  and handle retries. Use when: user requests batch transcription of local files,
  mentions scanning directories, inbox, or wants to retry failed items.
---

> **适配项目版本**：V0.4.24-Build0453-20260514

# 服务器本地文件批量转写

`funasr-task-manager-local-batch-transcribe` 是面向服务器本地文件的批量转写自动执行规程。它把"发现文件 → 预检查 → 批量提交 → 委托监控 → 结果归档 → 失败重试"固化为 Phase 0-7，智能体拿到触发词即可自主走完整个流程。

> **异步调度架构**：本 Skill 采用"主 Agent 调度 + 子 Agent 监控播报"模式。主 Agent 负责 Phase 0-4（扫描/预检/提交），完成提交后立即委托子 Agent 执行 `batch-monitor` Skill 进行进度监控和结果下载（Phase 5-6），自身释放以继续接收新任务。架构要点如下：
>
> - **职责分离**：主 Agent 控制扫描、预检、提交全流程；子 Agent 仅做"定期查询 + 播报 + 下载结果"，不参与意图理解和任务创建。
> - **委托协议**：主 Agent 通过 `sessions_spawn`（OpenClaw）或等效 API 启动子 Agent，传递 `task_group_ids`、`batch_id`、`notification_context` 等参数。
> - **Watchdog 机制**：主 Agent 维护 `batch_watchdog` 状态表，在被动（收到用户消息）或空闲时检查子 Agent 存活性，超时或失联时接管监控。
> - **Fallback**：当运行环境无子 Agent 能力（Cursor/Claude Code/Codex 等）或子 Agent 启动失败时，主 Agent 自行轮询。

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
# 群聊
python -m cli notify send --text "我将扫描 runtime/agent-local-batch/inbox/ 中的待转写文件..." --chat-id "<chat_id>" --reply-to "<reply_to_id>"
# 私聊
python -m cli notify send --text "我将扫描 runtime/agent-local-batch/inbox/ 中的待转写文件..." --receive-id-type open_id --chat-id "<open_id>"
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

**0a. 通知能力预检 + 路由验证（强制）**

在进入业务逻辑前，先检测通知能力并验证路由：

1. 检查 `message` tool 是否在工具列表中。
2. 执行 `python -m cli notify auth-check --channel feishu`，检查退出码。
3. 从 runtime context 提取 `notification_context`（`chat_id`、`open_id`、`is_group_chat` 等）。
4. **路由验证（首条通知）**：Phase 1 启动通知即为路由验证探针。发送后检查 `message` tool 返回的 `chatId`：
   - 匹配预期目标 → 继续使用 `message` tool
   - 不匹配 → 立即标记 `route_locked_to_cli = true`，通过 CLI notify 携带显式路由参数向正确目标重发同一条消息，后续全部走 CLI
   - 验证规则详见 `NOTICE-PRIMITIVE.md` §路由验证机制

预检输出：

```
notification_precheck:
  message_tool_available: true/false
  cli_notify_available: true/false
  is_group_chat: true/false
  adapter_priority: ["message", "cli_notify"] / ["cli_notify"] / ["assistant_text"]
  notice_capable: true/false
  route_locked_to_cli: false              # 首条通知后可能变为 true
  expected_chat_id: oc_xxx / ou_xxx       # 用于路由验证的预期目标
```

- 两者都不可用时：继续执行业务，但最终报告标注"⚠ 实时通知不可用"。
- 子 Agent 委托决策：如果 `notice_capable == false`，Phase 5 不启动子 Agent 监控播报，主 Agent 自行轮询。
- **路由锁定传递**：如果主 Agent 的路由验证触发了 CLI 锁定，Phase 5 委托子 Agent 时应在 `notification_context` 中额外传递 `prefer_cli_notify: true`，提示子 Agent 跳过 `message` tool 直接使用 CLI。

**0b. 仓库与后端检查**

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
| 本地去重依据 | 文件名 + 文件大小(bytes) + mtime(unix timestamp)，拼接为 `{name}-{size}-{mtime_int}` |
| 已完成跳过 | 同目录下最近 manifest 中标记 `SUCCEEDED` 且指纹未变化时跳过 |

> 后端 `task-group submit` 还会启用 30 分钟活跃批次去重，依据为同一用户下的文件名、大小、语言、热词/选项和 `segment_level`。检测到重复时 CLI 返回已有 `task_group_id`，Agent 应当进入监控复用流程，而不是再次提交。

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

默认去重行为：

- 30 分钟内同一用户、相同文件集和相同识别参数仍有活跃批次时，CLI 会返回已有 `task_group_id`，`total_deduplicated > 0`，退出码仍为 0。
- 确认需要重新创建独立批次时，追加 `--force`。
- 如果 submit 命令异常退出、输出损坏或 JSON 解析失败，禁止直接重试。必须先查询近 30 分钟任务组或后端日志确认是否已经创建批次；找到了就复用并监控，确认未创建后再重试。

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

#### 委托前提检查

主 Agent 在委托前必须检查以下条件，**任一不满足则不启动子 Agent 监控播报，直接 fallback 到主 Agent 自行轮询**：

1. **通知预检结果**：Phase 0a 的 `notice_capable == true`（至少有一种通知通道可用）。
2. **子 Agent 启动能力可用**：工具列表中包含 `sessions_spawn`（OpenClaw）或 `spawn_agent`（Codex）等子 Agent 启动工具。
3. **子 Agent 有消息工具**：`message` tool 需显式配置子 Agent 工具策略（`tools.subagents.tools.allow '["message"]'`），不自动继承；`cli notify` 可通过 `exec` 工具调用，子 Agent 自然继承此能力。

如果子 Agent 启动后发现自身 `message` tool 和 `cli notify` 都不可用，子 Agent 应立即报告失败并退出（见 `batch-monitor` Step 0）。

#### 委托协议

主 Agent 发送以下委托消息启动子 Agent（通过 `sessions_spawn`）：

```
请执行 batch-monitor 监控任务：
- task_group_ids: ["{group_id_1}", "{group_id_2}", ...]
- batch_id: {batch_id}
- total_files: {total_files}
- poll_interval_sec: 30
- timeout_sec: 3600
- result_format: txt
- output_dir: runtime/agent-local-batch/outputs/{batch_id}
- notification_context:
    chat_id: {chat_id}               # oc_xxx（群聊）
    open_id: {open_id}               # ou_xxx（私聊，可选）
    message_id: {message_id}         # om_xxx（触发消息 ID）
    reply_to_id: {reply_to_id}       # om_xxx（回复目标，可选）
    sender_id: {sender_id}           # ou_xxx（发送者 ID，用于 @ 提及）
    is_group_chat: {true/false}
- notification_policy:
    at_user_on_error: {true/false}   # 异常时 @ 触发用户（群聊 true，私聊 false）
    at_user_on_complete: {true/false}
    reply_to_thread: {true/false}    # 优先回复原消息线程
    template_variant: "group" / "dm" # 群聊 / 私聊模板
```

> **启动方式**：主 Agent 调用 `sessions_spawn` 启动子 Agent：
> ```json
> {"name": "sessions_spawn", "arguments": {"task": "请执行 batch-monitor 监控任务：...", "label": "batch-monitor-{batch_id}"}}
> ```

#### 委托后主 Agent 的行为

1. **发送委托通知**：通过 `send_user_notice()` 告知用户"已启动后台监控"。
2. **记录监控状态**：`batch_id -> {spawn_time, last_notice_at: null, child_session_key}`。
3. **等待启动确认（ack）**：使用 `sessions_yield` 等待子 Agent 的启动确认事件，或设 5 秒超时。
4. **5 秒内收到 ack**：释放控制权，继续接新任务。
5. **5 秒无 ack**：fallback 到主 Agent 自行轮询（见下方 Fallback）。

#### 子 Agent 负责的工作

子 Agent 按 `funasr-task-manager-batch-monitor` Skill 执行：

1. **工具权限自检**：检查 `message` tool 和 `cli notify` 可用性，两者都不可用则立即报告失败退出
2. 发送启动确认（ack）
3. 定期查询 `task-group status`
4. 通过 `send_user_notice()` 发送进度通知（每条记录 `adapter`、`message_id`、`delivered`）
5. 异常和完成时 @ 触发用户（群聊）/ 不 @（私聊）
6. 全部完成后执行 `task-group download` 下载结果
7. 发送完成汇总通知
8. 退出

#### Fallback：无子 Agent 能力时

以下任一条件成立时，主 Agent 回退到**自行轮询模式**：

- 运行环境无子 Agent 启动能力（如 Cursor / Claude Code 等无 `sessions_spawn` 或 `spawn_agent`）
- Phase 0a 预检 `notice_capable == false`（子 Agent 没有消息工具，无法播报）
- 子 Agent 启动后 5 秒无 ack
- 子 Agent 报告"消息工具不可用"并退出

```
while not all_groups_complete:
    for each group_id:
        python -m cli --output json task-group status {group_id}
    
    if progress_changed or elapsed > 30s:
        send_user_notice(进度通知)
    
    sleep 30
```

### Phase 5.5：Watchdog 监控（主 Agent 职责）

子 Agent 委托成功后，主 Agent 不主动轮询，但需维护 Watchdog 状态以防子 Agent 静默失联。

#### 监控状态表

主 Agent 维护以下状态，用于检测子 Agent 是否仍在正常播报：

```json
{
  "batch_watchdog": {
    "{batch_id}": {
      "spawn_time": "2026-05-07T15:30:00Z",
      "last_notice_at": null,
      "child_session_key": "batch-monitor-{batch_id}",
      "ack_received": false,
      "status": "spawned"
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `spawn_time` | 子 Agent 启动时间 |
| `last_notice_at` | 最近一次收到子 Agent 通知的时间（初始为 null） |
| `child_session_key` | 子 Agent 的 session label，用于 `sessions_list` 查询 |
| `ack_received` | 是否收到启动确认 |
| `status` | `spawned` → `acked` → `running` → `completed` / `failed` / `timeout` |

#### Watchdog 检查逻辑

主 Agent 在以下时机检查 Watchdog 状态：

1. **收到用户新消息时**（被动触发）：顺便检查 watchdog。
2. **当前无其他任务时**（空闲触发）：主动检查 watchdog。

```
for each batch_id in batch_watchdog:
  wd = batch_watchdog[batch_id]
  
  # 场景 A：启动后一直无通知
  if wd.status == "spawned" and now() - wd.spawn_time > 60s:
    # 子 Agent 可能已失败，主动查询
    sessions_list → 找到 child_session_key → 检查状态
    if child_exited or child_not_found:
      send_user_notice("⚠️ 批次 {batch_id} 的后台监控可能已中断，正在接管...")
      fallback 到主 Agent 自行轮询
  
  # 场景 B：曾有通知但长时间无更新（2 个轮询周期）
  if wd.last_notice_at and now() - wd.last_notice_at > poll_interval_sec * 2:
    # 检查子 Agent 是否还活着
    sessions_list → 找到 child_session_key
    if child_exited:
      # 子 Agent 退出但没发完成通知
      send_user_notice("⚠️ 批次 {batch_id} 的监控已结束，正在检查结果...")
      检查 task-group status → 补发完成/异常汇总
    elif child_running:
      # 子 Agent 还在但不发通知了，可能通知通道有问题
      记录 warning，继续等待
  
  # 场景 C：任务超时
  if now() - wd.spawn_time > timeout_sec * 1.2:
    send_user_notice("⚠️ 批次 {batch_id} 超时（已超过子 Agent 设定上限的 120%），检查状态中...")
    检查 task-group status → 补发汇总
```

#### 补发逻辑

| 场景 | 主 Agent 动作 |
|------|-------------|
| 子 Agent 已退出但未发完成通知 | 查询 `task-group status`，补发完成/异常汇总 |
| 子 Agent 已退出但结果未下载 | 执行 `task-group download`，补发结果 |
| 子 Agent 异常退出 | `send_user_notice("⚠️ 批次 {batch_id} 的后台监控异常中断。当前进度：{status_summary}。你可以说\"重启监控\"来恢复。")` |
| 全部完成但无汇总 | 补发完成汇总（使用 `progress-templates` 模板） |

### Phase 5.6：用户主动取消/中止批次

当用户明确要求"取消 / 停止 / 中止"正在运行的批次时，Agent 必须走取消流程，不能只终止监控子 Agent，也不能只停止心跳播报。

#### 取消步骤

1. 先通过 `send_user_notice()` 告知用户将取消哪个 `batch_id` / `task_group_id`。
2. 查询批次内任务列表：

```bash
python -m cli --output json task list --group {group_id} --page-size 500
```

3. 对每个非终态任务执行取消：

```bash
python -m cli --output json task cancel {task_id}
```

4. 取消后立即重新查询：

```bash
python -m cli --output json task-group status {group_id}
```

5. 只有当 `is_complete == true`，或 `succeeded + failed + canceled == total` 时，才向用户报告"取消完成"。
6. 如果仍有 `in_progress > 0`，必须报告"取消已提交但仍有活跃任务"，并继续每 10-30 秒检查，直到进入终态或超时。

#### segment 清理校验

后端取消接口必须释放分段任务的活跃 segment，包括 `PENDING`、`DISPATCHED`、`TRANSCRIBING`。Agent 侧取消后如发现某台服务器长期少 slot、某个 group 已取消但仍有 `TRANSCRIBING` segment，应按故障处理上报，不要反复新建批次掩盖问题。

必要时使用只读诊断确认是否存在僵尸 segment：

```sql
SELECT s.segment_id, s.task_id, s.status, s.assigned_server_id, t.status AS parent_status
FROM task_segments s
JOIN tasks t ON t.task_id = s.task_id
WHERE s.status IN ('DISPATCHED', 'TRANSCRIBING')
  AND t.status IN ('CANCELED', 'FAILED', 'SUCCEEDED');
```

如果查询命中，说明后端取消/恢复流程存在 bug；Agent 应报告异常并请求人工确认后再做数据库修复。

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
