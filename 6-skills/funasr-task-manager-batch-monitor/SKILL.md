---
name: funasr-task-manager-batch-monitor
description: >
  Sub-agent skill for monitoring batch transcription progress and sending
  periodic notifications via message tool. Binds to a task_group_id,
  polls status at fixed intervals, and sends progress/completion/error
  notifications using fixed templates. Designed to run as a delegated
  sub-agent so the main agent can continue accepting new tasks.
---

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
```

---

## 执行检查清单（强制）

子 Agent 在执行本 Skill 时，必须严格按以下顺序执行，不可跳步、合并或自由发挥。

| # | 检查项 | 时机 | 模板引用 | 操作 |
|---|--------|------|---------|------|
| 1 | 启动确认 | 收到委托后立即 | `monitor-templates.md` §1 | 确认参数并通知用户 |
| 2 | 定期进度播报 | 每 poll_interval_sec | `monitor-templates.md` §2 | 查状态 → 有变化则通知 |
| 3 | 心跳播报 | 无变化超过 heartbeat_interval_sec | `monitor-templates.md` §3 | 告知用户仍在运行 |
| 4 | 异常播报 | 检测到失败任务时 | `monitor-templates.md` §4 | 通知异常详情 |
| 5 | 结果下载 | 全部完成时 | — | CLI: task-group download |
| 6 | 完成汇总 | 下载完成后 | `monitor-templates.md` §5 | 发送完成通知并退出 |

---

## 核心执行流程

### Step 0：参数校验

1. 确认工作目录是 `funasr-task-manager` 仓库。
2. 检查后端健康状态：

```bash
curl -sf http://127.0.0.1:15797/health
```

3. 后端不可达则立即通知用户并退出（不尝试启动——这不是子 Agent 的职责）。
4. 确认所有 `task_group_ids` 存在：

```bash
cd 3-dev/src/backend
python -m cli --output json task-group status {group_id}
```

5. 不存在的 group 记录为错误，从监控列表中移除。全部不存在则通知用户并退出。

### Step 1：发送启动确认

**必须**通过 `send_user_notice()` 发送启动通知，套用 `monitor-templates.md` §1。

### Step 2：进度轮询循环

```
初始化:
  last_succeeded = 0
  last_failed = 0
  last_heartbeat_time = now()
  start_time = now()

循环:
  # 2a. 超时检测
  if now() - start_time > timeout_sec:
    send_user_notice(超时通知)
    进入 Step 5（强制下载已完成部分）

  # 2b. 查询状态（每个 group 独立查询）
  for each group_id in task_group_ids:
    result = CLI: task-group status {group_id} --output json
    累加 succeeded, failed, in_progress, total

  # 2c. 判断是否有新进展
  if succeeded != last_succeeded or failed != last_failed:
    send_user_notice(进度通知, 套用 monitor-templates §2)
    last_succeeded = succeeded
    last_failed = failed
    last_heartbeat_time = now()

  # 2d. 心跳检测（长时间无变化）
  elif now() - last_heartbeat_time > heartbeat_interval_sec:
    send_user_notice(心跳通知, 套用 monitor-templates §3)
    last_heartbeat_time = now()

  # 2e. 判断是否全部完成
  if succeeded + failed == total:
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

### Step 5：退出

子 Agent 在以下任一条件满足时退出：

| 退出条件 | 退出前动作 |
|---------|----------|
| 全部任务完成（succeeded + failed == total） | 下载结果 → 发完成通知 |
| 超时（elapsed > timeout_sec） | 发超时通知 → 下载已完成部分 |
| 后端不可达（连续 3 次查询失败） | 发异常通知 |
| 全部 group 不存在 | 发错误通知 |

---

## 通知规则

### `send_user_notice()` 调用方式

**OpenClaw 环境（首选）：**

```json
{"name": "message", "arguments": {"action": "send", "message": "<模板渲染后的文本>"}}
```

**CLI fallback（无 message tool 时）：**

```bash
python -m cli notify send --text "<模板渲染后的文本>"
```

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
