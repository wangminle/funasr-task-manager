# Agent 实时通知使用指南

> 本文件是 `CHANNEL-NOTIFICATION.md` 的实操补充，提供各平台的具体调用示例和故障排查指引。

---

## OpenClaw 环境

### 判断 message tool 是否可用

Agent 在 session 启动时可见的工具列表中查找 `message`。如果存在，则整个 session 期间优先使用它。

### 发送文本通知

```json
{
  "name": "message",
  "arguments": {
    "action": "send",
    "message": "⏳ 正在从飞书下载文件..."
  }
}
```

### 发送文件附件

```json
{
  "name": "message",
  "arguments": {
    "action": "send",
    "message": "✅ 转写完成\n\n  文件名: 会议录音.mp4\n  音频时长: 3m 12s\n  转写耗时: 45s\n  文本长度: 2841 字\n  结果文件: 会议录音.txt\n\n  📎 结果文件已发送，请查收。",
    "filePath": "/tmp/funasr-task-manager/results/会议录音.txt"
  }
}
```

### 成功结果解析

```json
{
  "ok": true,
  "channel": "feishu",
  "action": "send",
  "messageId": "om_d4be107c9a2c3ef6a6fbe1c7xxxx",
  "chatId": "oc_5ad11d72b830411d72b836c2xxxx"
}
```

- `ok == true`：消息已送达飞书
- `messageId`：可用于后续回复线程
- `chatId`：当前会话 ID

### 失败结果

```json
{
  "ok": false,
  "error": "channel_not_connected",
  "message": "Feishu channel is not connected for this agent"
}
```

失败时记录错误，默认不阻塞主流程。

---

## CLI notify 命令（fallback）

### 前置条件

1. 凭据已配置（任选一种方式）：

```bash
# 方式 A：环境变量
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
export FEISHU_CHAT_ID=oc_xxx

# 方式 B：CLI 配置文件 (~/.asr-cli.yaml)
python -m cli config set notify.feishu_app_id cli_xxx
python -m cli config set notify.feishu_app_secret xxx
python -m cli config set notify.default_chat_id oc_xxx
```

2. 验证凭据可用：

```bash
python -m cli notify auth-check
# 成功输出: 飞书凭据有效 (app_id: cli_xxx, token 已缓存...)
# 失败输出: Error: 飞书凭据无效: {error_detail}  (exit 1)
```

### 发送文本

```bash
# 简单文本
python -m cli notify send --text "⏳ 正在从飞书下载文件..."

# 指定目标会话（覆盖默认）
python -m cli notify send --text "✅ 转写完成" --chat-id oc_xxx

# 回复特定消息（线程内回复）
python -m cli notify send --text "⏳ 处理中..." --reply-to om_xxx

# 多行文本（避免 shell 转义问题）
python -m cli notify send --text-file /tmp/notice.txt

# 从 stdin 读取
echo "批量转写进度：35/50 已完成" | python -m cli notify send --stdin
```

### 退出码

| 场景 | 退出码 | stderr |
|------|--------|--------|
| 发送成功 | 0 | 无 |
| 发送失败（soft-fail） | 0 | `[WARN] 通知发送失败: {reason}` |
| 发送失败（--strict） | 1 | `Error: 通知发送失败: {reason}` |
| 凭据缺失 | 0 | `[WARN] 通知凭据未配置，跳过发送` |
| auth-check 失败 | 1 | `Error: 飞书凭据无效: {detail}` |

### stdout 输出（成功时）

```
message_id=om_d4be107c9a2c3ef6xxxx
```

Agent 可捕获 message_id 用于后续回复线程。

---

## 各平台调用方式

### OpenClaw

```
Tool: message
Arguments: {"action": "send", "message": "..."}
```

直接使用平台暴露的 `message` tool。会话上下文（chat_id、thread）由平台自动管理。

### Hermes

如果 Hermes 暴露类似的 message tool，使用方式相同。否则使用 CLI fallback。

### Cursor / Claude Code

CLI 调用：

```bash
python -m cli notify send --text "..." 
```

工作目录：`3-dev/src/backend`

如果是纯本地开发场景（用户直接看 Agent 输出），可退化为普通 assistant 文本。但如果配置了飞书凭据且有 `FEISHU_CHAT_ID`，应使用 CLI 发送。

### Codex

CLI 调用（沙箱环境）：

```bash
cd 3-dev/src/backend && python -m cli notify send --text "..."
```

注意 Codex 沙箱可能无法访问外网飞书 API。此时 soft-fail 会静默跳过。

---

## 故障排查

| 症状 | 可能原因 | 解决 |
|------|---------|------|
| message tool 不在工具列表中 | Agent 配置未启用 messaging | 检查 OpenClaw agent 配置中的 `tools` 列表 |
| `ok: false` + `channel_not_connected` | 飞书 channel 断连 | 检查 OpenClaw 飞书 websocket 连接状态 |
| CLI notify 静默失败 | 凭据未配置 | 运行 `python -m cli notify auth-check` 定位问题 |
| 消息送达但不在原话题线程 | 未配置 reply_to | OpenClaw message tool 通常自动在当前线程；CLI 需要显式 `--reply-to` |
| 飞书报 `code: 99991668` | Token 过期且刷新失败 | 检查 app_secret 是否正确，手动删除 `~/.asr-cli-feishu-token.json` 重试 |
| 消息内容乱码 | JSON 编码问题 | 使用 `--text-file` 或 `--stdin` 替代 `--text` 避免 shell 编码问题 |

---

## 通知内容规范

通知文本必须来自对应 Skill 的模板文件（如 `progress-templates.md`、`response-templates.md`），不由 Agent 自由生成。

格式要求：
- 阶段标识符开头（Phase X / 步骤说明）
- 进度数值具体化（3/5、35%）
- 时间估算有数据支撑（基于 RTF 和音频时长）
- 不包含 JSON / Markdown 表格等复杂格式（飞书文本消息不渲染）
