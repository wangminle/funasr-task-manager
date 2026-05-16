---
name: funasr-task-manager-batch-monitor
description: >
  Sub-agent skill for monitoring batch transcription progress and sending
  periodic notifications via message tool. Binds to a task_group_id,
  polls status at fixed intervals, and sends progress/completion/error
  notifications using fixed templates. Designed to run as a delegated
  sub-agent so the main agent can continue accepting new tasks.
---

> **适配项目版本**：V0.4.25-Build0454-20260516

# 批量转写进度监控（子 Agent 专用）

`funasr-task-manager-batch-monitor` 是子 Agent 专用的监控播报规程。它绑定一个或多个 `task_group_id`，按固定间隔查询状态，套用固定模板通过 `send_user_notice()` 发送进度通知，直到全部任务完成或超时。

> **核心定位**：本 Skill 的执行者是**子 Agent**，不是主 Agent。主 Agent 通过 `local-batch-transcribe` 完成扫描和提交后，启动子 Agent 执行本 Skill，自身释放去接新任务。

> **实时通知规范**：本 Skill 的所有用户通知必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。禁止用普通文本替代 `send_user_notice()`。

---

## 触发条件

本 Skill **不由用户直接触发**，仅由主 Agent 在以下场景委托启动：

- `local-batch-transcribe` Phase 4 提交完成后
- 用户要求恢复一个中断的批次监控
- 主 Agent 检测到有已提交但无监控的任务组

### 启动参数

主 Agent 启动子 Agent 时必须传递以下参数：

| 参数 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `task_group_ids` | string[] | 是 | 一个或多个需监控的任务组 ID |
| `batch_id` | string | 是 | 本地批次 ID（用于通知模板和结果归档路径） |
| `total_files` | int | 是 | 文件总数（用于进度分母） |
| `poll_interval_sec` | int | 否 | 轮询间隔，默认 30 |
| `heartbeat_interval_sec` | int | 否 | 无变化时的心跳通知间隔，默认 60 |
| `timeout_sec` | int | 否 | 最大运行时间，默认 3600 |
| `result_format` | string | 否 | 结果下载格式，默认 "txt" |
| `output_dir` | string | 否 | 结果归档目录，默认 `runtime/agent-local-batch/outputs/{batch_id}` |

#### `notification_context`（必需）

主 Agent 必须将当前会话的通知上下文传递给子 Agent，子 Agent 据此决定向哪个渠道发送通知：

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `chat_id` | string | 条件 | 目标群聊 ID（`oc_xxx`），群聊场景必须；私聊时可省略 |
| `open_id` | string | 条件 | 用户 Open ID（`ou_xxx`），私聊场景必须；群聊时可省略（但仍用于 @ 提及） |
| `message_id` | string | 否 | 原始触发消息 ID（`om_xxx`），用于回复线程 |
| `reply_to_id` | string | 否 | 回复目标消息 ID，优先回复此线程 |
| `sender_id` | string | 否 | 触发用户的 Open ID（`ou_xxx`），用于 @ 提及 |
| `is_group_chat` | bool | 是 | `true` = 群聊，`false` = 私聊 |
| `prefer_cli_notify` | bool | 否 | `true` = 主 Agent 已检测到 `message` tool 路由异常，建议子 Agent 直接使用 CLI notify |

#### `notification_policy`（必需）

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `at_user_on_error` | bool | true（群聊）/ false（私聊） | 异常时是否 @ 触发用户 |
| `at_user_on_complete` | bool | true（群聊）/ false（私聊） | 完成时是否 @ 触发用户 |
| `reply_to_thread` | bool | true | 优先回复原消息线程 |
| `template_variant` | string | 按 `is_group_chat` 自动 | `"group"` 或 `"dm"` |

**启动参数传递方式**：主 Agent 在委托消息中以结构化文本传递这些参数。示例：

```
请执行 batch-monitor 监控任务：
- task_group_ids: ["01JD3KXYZ0000000000000001"]
- batch_id: local-20260506-143000
- total_files: 50
- poll_interval_sec: 30
- timeout_sec: 3600
- result_format: txt
- output_dir: runtime/agent-local-batch/outputs/local-20260506-143000
- notification_context:
    chat_id: oc_xxxxxxxxxxxxxxxx
    open_id: ou_xxxxxxxxxxxxxxxx
    message_id: om_xxxxxxxxxxxxxxxx
    sender_id: ou_xxxxxxxxxxxxxxxx
    is_group_chat: true
- notification_policy:
    at_user_on_error: true
    at_user_on_complete: true
    reply_to_thread: true
    template_variant: group
```

---

## 执行检查清单（强制）

子 Agent 在执行本 Skill 时，必须严格按以下顺序执行，不可跳步、合并或自由发挥。

| # | 检查项 | 时机 | 模板引用 | 操作 |
|---|--------|------|---------|------|
| 0 | **工具权限自检** | 收到委托后立即（Step 0 前） | — | 检查 `message` tool 和 `cli notify`，两者都不可用则报告失败退出 |
| 1 | **启动确认 (ack)** | 自检通过后 5 秒内 | `monitor-templates.md` §1 | 确认参数并通知用户，记录 `notice_log[0]` |
| 2 | 定期进度播报 | 每 poll_interval_sec | `monitor-templates.md` §2 | 查状态 → 有变化则通知，记录 `notice_log` |
| 3 | 心跳播报 | 无变化超过 heartbeat_interval_sec | `monitor-templates.md` §3 | 告知用户仍在运行，记录 `notice_log` |
| 4 | 异常播报 | 检测到失败任务时 | `monitor-templates.md` §4 | 通知异常详情（群聊 @ 用户），记录 `notice_log` |
| 5 | 结果下载 | 全部完成时 | — | CLI: task-group download |
| 6 | **完成/异常汇总** | 下载完成后或异常退出前 | `monitor-templates.md` §5 | 发送完成通知（含通知统计），**子 Agent 不可静默退出** |

### 通知日志格式 (`notice_log`)

子 Agent 必须维护通知发送记录，用于完成汇总中的"通知统计"：

```json
{
  "notice_log": [
    {"seq": 1, "type": "ack", "adapter": "message", "message_id": "om_xxx", "delivered": true, "ts": "..."},
    {"seq": 2, "type": "progress", "adapter": "cli-notify", "message_id": null, "delivered": true, "ts": "..."},
    {"seq": 3, "type": "error", "adapter": "message", "message_id": "om_yyy", "delivered": false, "ts": "..."}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `seq` | 通知序号，从 1 递增 |
| `type` | `ack` / `progress` / `heartbeat` / `error` / `timeout` / `complete` |
| `adapter` | `message` / `cli-notify` / `text-only` |
| `message_id` | 发送成功时的消息 ID（若平台返回） |
| `delivered` | `true` / `false` |
| `ts` | ISO 8601 时间戳 |

---

## 核心执行流程

### Step 0：工具权限自检 + 路由验证 + 参数校验

#### 0a. 通知工具权限自检（强制，最先执行）

子 Agent 启动后第一步必须确认自身是否拥有向用户发送消息的能力：

1. **检查 `message` tool**：尝试在工具列表中确认 `message` 工具存在。
2. **检查 `cli notify`**（两步：先确认模块存在，再验证凭据有效）：

```bash
# 第一步：确认 CLI 模块存在
cd 3-dev/src/backend && python -m cli notify --help > /dev/null 2>&1
# 第二步（如果第一步成功）：验证凭据可用
python -m cli notify auth-check --channel feishu
```

3. **检查 `prefer_cli_notify` 提示**：如果 `notification_context.prefer_cli_notify == true`，说明主 Agent 已检测到 `message` tool 路由异常，子 Agent 应直接跳过 `message` tool，锁定使用 CLI notify。

4. **判定结果**：

| `message` tool | `cli notify` | `prefer_cli_notify` | 结论 |
|:-:|:-:|:-:|------|
| ✅ | ✅ | false | 优先用 `message`（Step 1 ack 时做路由验证），`cli notify` 备用。|
| ✅ | ✅ | true | **直接锁定 `cli notify`**，跳过 `message` tool。|
| ✅ | ❌ | — | 仅用 `message`（ack 时做路由验证）。|
| ❌ | ✅ | — | 仅用 `cli notify`。|
| ❌ | ❌ | — | **立即报告失败退出**。|

5. 记录自检结果：`adapter = "message" | "cli-notify"`，`route_locked_to_cli = prefer_cli_notify`。

#### 0a-2. 路由验证（ack 消息兼做路由探针）

如果选定 `message` tool 且未锁定到 CLI：

1. Step 1 的 ack 通知即为路由验证探针
2. 发送 ack 后检查返回的 `chatId` 是否与 `notification_context` 中的预期目标一致
3. **不匹配**：标记 `route_locked_to_cli = true`，通过 CLI notify 向正确目标重发 ack 消息，后续全部走 CLI
4. **匹配**：继续使用 `message` tool

验证规则详见 `NOTICE-PRIMITIVE.md` §路由验证机制。

#### 0b. 参数校验

1. 确认工作目录是 `funasr-task-manager` 仓库。
2. 解析 `notification_context`，确认 `chat_id`（群聊）或 `open_id`（私聊）存在。
3. 解析 `notification_policy`，确定模板变体（`group` / `dm`）。
4. 检查后端健康状态：

```bash
curl -sf http://127.0.0.1:15797/health
```

5. 后端不可达则通过 `send_user_notice()` 通知用户并退出（不尝试启动——这不是子 Agent 的职责）。
6. 确认所有 `task_group_ids` 存在：

```bash
cd 3-dev/src/backend
python -m cli --output json task-group status {group_id}
```

7. 不存在的 group 记录为错误，从监控列表中移除。全部不存在则通知用户并退出。

### Step 1：发送启动确认（ack）

**必须在 Step 0 完成后 5 秒内**通过 `send_user_notice()` 发送启动通知，套用 `monitor-templates.md` §1。

- 使用 Step 0a 确定的 `adapter` 发送。
- 发送后记录 `notice_log[0]`（type=ack, adapter, delivered, ts）。
- 若 `notification_policy.reply_to_thread == true` 且 `reply_to_id` 或 `message_id` 存在，优先回复原消息线程。

主 Agent 依赖此 ack 消息判断子 Agent 是否成功启动。**未能发送 ack 的子 Agent 将被主 Agent 判定为启动失败。**

### Step 2：进度轮询循环

```
初始化:
  last_succeeded = 0
  last_failed = 0
  last_canceled = 0
  last_heartbeat_time = now()
  start_time = now()

循环:
  # 2a. 超时检测
  if now() - start_time > timeout_sec:
    send_user_notice(超时通知)
    进入 Step 5（强制下载已完成部分）

  succeeded = 0
  failed = 0
  canceled = 0
  in_progress = 0
  total = 0
  all_complete = true

  # 2b. 查询状态（每个 group 独立查询）
  for each group_id in task_group_ids:
    result = CLI: task-group status {group_id} --output json
    累加 succeeded, failed, canceled, in_progress, total
    all_complete = all_complete and result.is_complete

  # 2c. 判断是否有新进展
  if succeeded != last_succeeded or failed != last_failed or canceled != last_canceled:
    send_user_notice(进度通知, 套用 monitor-templates §2)
    last_succeeded = succeeded
    last_failed = failed
    last_canceled = canceled
    last_heartbeat_time = now()

  # 2d. 心跳检测（长时间无变化）
  elif now() - last_heartbeat_time > heartbeat_interval_sec:
    send_user_notice(心跳通知, 套用 monitor-templates §3)
    last_heartbeat_time = now()

  # 2e. 判断是否全部完成
  if all_complete:
    退出循环，进入 Step 3

  # 2f. 等待
  sleep(poll_interval_sec)
```

**CLI 命令**（工作目录：`3-dev/src/backend`）：

```bash
python -m cli --output json task-group status {group_id}
```

返回 JSON：

```json
{
  "task_group_id": "...",
  "status": "RUNNING",
  "total": 50,
  "succeeded": 35,
  "failed": 2,
  "canceled": 1,
  "in_progress": 13,
  "is_complete": false
}
```

### Step 3：结果下载

全部任务完成后，下载已成功任务的结果：

```bash
python -m cli --output json task-group download {group_id} \
  --format {result_format} \
  --output-dir {output_dir}
```

对每个 `task_group_id` 分别执行。

### Step 4：发送完成汇总

通过 `send_user_notice()` 发送完成通知，套用 `monitor-templates.md` §5。

如果有结果文件需要发送给用户（如汇总文件），使用 `filePath` 参数：

```json
{"name": "message", "arguments": {"action": "send", "message": "...", "filePath": "..."}}
```

### Step 5：退出（不可静默退出）

子 Agent 在以下任一条件满足时退出。**所有退出路径必须发送汇总通知，不可静默退出。**

| 退出条件 | 退出前动作 |
|---------|----------|
| 全部任务完成（`is_complete == true`，等价于 `succeeded + failed + canceled == total`） | 下载结果 → 发完成汇总（含通知统计） |
| 超时（elapsed > timeout_sec） | 发超时汇总（含通知统计） → 下载已完成部分 |
| 后端不可达（连续 3 次查询失败） | 发异常汇总（含通知统计） |
| 全部 group 不存在 | 发错误汇总 |
| 工具权限自检失败（Step 0a） | 通过 `completion announce` 报告"消息工具不可用" |

#### 汇总中的通知统计

完成/异常汇总模板中必须包含通知发送统计：

```
📤 通知统计：{notice_sent} 条已送达，{notice_failed} 条未送达
```

统计数据从 `notice_log` 中计算：
- `notice_sent = count(notice_log where delivered == true)`
- `notice_failed = count(notice_log where delivered == false)`

#### 退出清单

子 Agent 退出前必须完成以下检查：

1. ✅ 发送了汇总通知（完成/异常/超时/错误）
2. ✅ 汇总包含通知统计
3. ✅ 群聊场景下汇总 @ 了触发用户
4. ✅ 无未处理的下载结果

---

## 通知规则

### `send_user_notice()` 调用方式

**OpenClaw 环境（首选）：**

```json
{"name": "message", "arguments": {"action": "send", "message": "<模板渲染后的文本>"}}
```

**CLI fallback（无 message tool 时）：**

```bash
python -m cli notify send --text "<模板渲染后的文本>" --chat-id "<chat_id>"
```

私聊使用 `--receive-id-type open_id --chat-id "<open_id>"`。子 Agent 必须使用主 Agent 传入的 `notification_context`，不得依赖默认 `FEISHU_CHAT_ID`。

### 频率控制

| 通知类型 | 频率 |
|---------|------|
| 进度更新 | 有变化时立即发送，但不超过每 poll_interval_sec 一次 |
| 心跳 | 无变化超过 heartbeat_interval_sec 时发送，不超过每 60s 一次 |
| 异常 | 每个失败任务只通知一次 |
| 完成汇总 | 仅一次 |

### 去重规则

子 Agent 必须维护以下状态变量，防止重复通知：

- `last_succeeded`：上次通知时的成功数
- `last_failed`：上次通知时的失败数
- `notified_failed_tasks`：已通知过的失败任务 ID 集合

只有当 `succeeded` 或 `failed` 发生变化时才发送进度通知。

---

## 错误处理

| 错误场景 | 子 Agent 应做的事 | 不应做的事 |
|---------|-----------------|----------|
| 后端查询失败（单次） | 记录警告，下一轮重试 | 立即退出或通知用户 |
| 后端连续 3 次失败 | 通知用户后端异常，退出 | 无限重试 |
| 某个 group 404 | 从监控列表移除，通知用户 | 反复查询已不存在的 group |
| 下载结果失败 | 通知用户下载失败，给出手动命令 | 静默跳过 |
| message tool 发送失败 | 记录失败，继续执行，最终汇总标注 | 中断监控流程 |

---

## 安全与边界规则

1. 子 Agent **只做监控和播报**，不做任务创建、文件上传或意图理解。
2. 子 Agent **不响应用户对话**——如果用户在群聊中说了新的指令，那是主 Agent 的事。
3. 子 Agent **不修改 manifest**——manifest 由主 Agent 管理。
4. 子 Agent 检测到任务终态后**必须退出**，不能无限运行。
5. 子 Agent 的 `timeout_sec` 是硬上限，到时间必须退出。

---

## 与其他 Skill 的协作

| 协作场景 | 对方 Skill | 交互方式 |
|---------|-----------|---------|
| 主 Agent 委托监控 | `local-batch-transcribe` | 主 Agent 传递 task_group_ids 和 batch_id |
| 结果下载 | — | 复用 CLI `task-group download` |
| 通知模板 | `_shared/CHANNEL-NOTIFICATION.md` | 遵循统一通知规范 |

---

## 相关文件

- `references/monitor-templates.md`：子 Agent 通知模板
- `6-skills/_shared/CHANNEL-NOTIFICATION.md`：渠道通知规范
- `6-skills/_shared/ASR-WORKFLOW.md`：ASR 工作流总览
