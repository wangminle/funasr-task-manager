# 渠道实时通知规范

> 本文件定义 Agent 向用户发送实时状态通知的强制规则。所有 Skill 中出现"通知用户 / 汇报 / 反馈 / 进度更新"的位置，都必须遵循本规范。
>
> **背景**：普通 assistant 文本输出在 OpenClaw/Hermes 等聊天平台中会被 turn 级缓冲，直到整个工具调用链结束后才推送到飞书/企业微信/Slack。因此普通文本 ≠ 实时通知。只有通过显式工具调用（message tool 或 CLI notify）产生的副作用才能绕过缓冲、立即送达用户。

---

## 强制规则

1. **所有用户可见状态同步必须通过 `send_user_notice()` 实现。**
2. **禁止**把阶段通知仅写入普通 assistant 文本回复。
3. **禁止**在长耗时工具调用前不发送状态消息。
4. **每次 `send_user_notice()` 必须发生在下一次耗时操作之前。**
5. **`send_user_notice()` 等待返回后再继续下一步工具调用。**
6. **禁止**同时使用 message tool 和 CLI notify 发送同一条通知（避免重复消息）。

---

## `send_user_notice()` 适配器选择

按以下优先级选择实现方式。**只使用第一个可用的方式，成功后不再 fallback。**

```
优先级 1：平台原生 message tool（OpenClaw / Hermes）
优先级 2：CLI notify 命令（python -m cli notify send）
优先级 3：普通 assistant 文本（仅当确认运行在纯本地终端时）
```

### 优先级 1：OpenClaw / Hermes `message` tool

当 runtime 暴露 `message` tool 时（Agent 可在工具列表中看到它），**必须**使用它发送通知。

**发送文本通知：**

```json
{
  "name": "message",
  "arguments": {
    "action": "send",
    "message": "⏳ 正在从飞书下载文件..."
  }
}
```

**发送文本 + 文件附件：**

```json
{
  "name": "message",
  "arguments": {
    "action": "send",
    "message": "✅ 转写完成，结果文件已发送。",
    "filePath": "/tmp/funasr-task-manager/results/会议录音.txt"
  }
}
```

**成功判断：** toolResult 中 `ok == true` 视为送达成功。

```json
{"ok": true, "channel": "feishu", "action": "send", "messageId": "om_xxx", "chatId": "oc_xxx"}
```

**失败处理：** `ok != true` 或 tool 执行报错时，记录失败但默认不阻塞主流程。在最终报告中注明"以下 N 条通知未送达"。

### 优先级 2：CLI notify 命令

仅当以下条件之一成立时使用：

- runtime 没有可调用的 `message` tool
- `message` tool 连续失败且部署方允许使用飞书 API 凭据兜底
- 在非聊天平台环境（Cursor / Codex）中运行 Skill 但仍需发飞书通知

**发送文本通知：**

```bash
python -m cli notify send --text "⏳ 正在从飞书下载文件..."
```

**发送多行/复杂文本（避免 shell 转义问题）：**

```bash
echo "⏳ 文件预处理中...
  格式: WAV 16kHz 单声道
  时长: 约 3 分钟" | python -m cli notify send --stdin
```

**发送文件附件：**

```bash
python -m cli notify send-file --file /tmp/funasr-task-manager/results/会议录音.txt --filename "会议录音.txt"
```

**退出码：** 默认 soft-fail（exit 0 + stderr warning）。主流程不因通知失败而中断。

### 优先级 3：普通 assistant 文本

**仅当确认以下条件全部成立时可用：**

- 当前运行环境是纯本地终端（无聊天平台桥接）
- 用户直接看到 Agent 实时输出流
- 不存在 turn 缓冲

典型场景：用户在本地终端通过 Cursor / Claude Code 直接交互。

---

## 通知时序规则

```
正确顺序：
  send_user_notice("⏳ 正在下载文件...")  ← 先通知
  curl 下载文件                           ← 再执行耗时操作
  send_user_notice("✅ 下载完成")         ← 完成后通知

错误顺序：
  curl 下载文件                           ← 直接执行
  "正在下载文件..."                       ← 缓冲在 assistant 文本中，用户看不到
```

每个 Skill Phase 的模式必须是：

1. `send_user_notice()` 告知即将做什么
2. 执行耗时操作
3. `send_user_notice()` 告知结果（可选，视 Phase 持续时间决定）

---

## 频率控制

| 场景 | 通知频率上限 | 说明 |
|------|------------|------|
| 单文件转写各阶段 | 每个 Phase 1 次 | 下载/预检/上传/提交/完成 |
| 批量转写进度 | 每 30 秒或每 5 个文件完成 | 避免飞书 API 限流（50 QPS） |
| 状态变化 | 每次变化 1 次 | PREPROCESSING→QUEUED→TRANSCRIBING→SUCCEEDED |
| benchmark 事件 | phase_start + phase_complete | 中间 gradient 静默除非用户要求详细日志 |
| 心跳（无新变化） | 每 60 秒最多 1 次 | 长任务时告知用户"仍在运行" |

---

## 失败与降级

| 情况 | 处理 |
|------|------|
| message tool 返回 `ok: false` | 记录失败，继续主流程，最终报告中标注 |
| CLI notify exit 非零（`--strict`） | 记录失败；只有调用方明确要求严格模式时才中断主流程 |
| 凭据未配置 | stderr 输出"通知通道未配置"，继续主流程 |
| 全部通知通道不可用 | 退化为普通 assistant 文本 + 最终报告注明"本次通知通道不可用" |
| 通知成功但用户可能未读 | 不重发；按"发送即完成"原则 |

---

## 子 Agent 通知规则

当主 Agent 委托子 Agent 进行进度监控时（如批量转写的 `batch-monitor` Skill），子 Agent **同样必须遵循本规范**的所有规则。

### 子 Agent 特殊规则

1. **子 Agent 必须通过 `send_user_notice()` 发送所有通知**，与主 Agent 使用相同的适配器选择逻辑。
2. **子 Agent 不输出对话内容**——所有输出仅限于固定模板通知，不做闲聊或自由文本生成。
3. **子 Agent 的通知必须包含批次标识**（如 `batch_id`），以便用户区分不同批次的通知。
4. **多个子 Agent 并发时**，各自独立发送通知，通知内容通过 batch_id / group_id 区分。
5. **子 Agent 退出前必须发送一条完成或异常汇总通知**，不可静默退出。

### 主 Agent 与子 Agent 的通知分工

| 通知类型 | 负责方 | 说明 |
|---------|--------|------|
| 扫描开始/完成 | 主 Agent | Phase 1-2 |
| 提交确认 | 主 Agent | Phase 4 |
| 监控启动确认 | 子 Agent | 收到委托后立即发送 |
| 定期进度更新 | 子 Agent | 每 30s 或有变化时 |
| 心跳通知 | 子 Agent | 长时间无变化时 |
| 异常通知 | 子 Agent | 任务失败/后端异常 |
| 完成汇总 | 子 Agent | 全部完成后 |
| 新任务受理 | 主 Agent | 子 Agent 播报期间收到新任务 |

---

## 引用方式

在 Skill 文件顶部添加：

```markdown
> **实时通知规范**：本 Skill 的所有用户通知必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。禁止用普通文本替代 `send_user_notice()`。
```
