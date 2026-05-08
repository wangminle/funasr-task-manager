# `send_user_notice()` — 可执行通知原语

> 本文件是所有 Skill 发送用户通知的唯一实现参考。Skill 文件只需声明**通知内容**和**时机**，调用方式和适配器选择逻辑统一引用本文件。

---

## 调用签名

```
send_user_notice(text, filePath=None)
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `text` | string | 通知正文（支持 emoji 前缀表示状态） |
| `filePath` | string \| null | 可选。文件附件的绝对路径（发送结果 txt/zip 时使用） |

**返回**：送达成功 → 记录 `message_id`；失败 → 记录失败原因但默认不阻塞主流程。

---

## 适配器选择（按优先级，只用第一个成功的）

### 优先级 1：平台 `message` tool

条件：Agent 工具列表中存在 `message` tool（OpenClaw / Hermes 环境）。

**发送文本：**

```json
{"name": "message", "arguments": {"action": "send", "message": "<text>"}}
```

**发送文本 + 文件附件：**

```json
{"name": "message", "arguments": {"action": "send", "message": "<text>", "filePath": "<filePath>"}}
```

**成功判断：** `toolResult.ok == true`

### 优先级 2：CLI notify

条件：无 `message` tool，或 `message` tool 连续失败且已配置飞书凭据。

**发送文本：**

```bash
python -m cli notify send --text "<text>"
```

**发送文件附件：**

```bash
python -m cli notify send-file --file "<filePath>" --filename "<display_name>"
```

**成功判断：** exit 0 且 stdout 输出 `message_id=om_xxx`

### 优先级 3：普通 assistant 文本

条件：确认当前运行在纯本地终端（无聊天平台桥接），用户直接看到 Agent 实时输出。

此优先级**不可**在 OpenClaw/Hermes 聊天平台中使用。

---

## 路由上下文

Agent session 启动时提取 `notification_context`（来自 runtime context）：

```
chat_id        → 群聊 ID (oc_xxx)，去除 "chat:" 前缀
open_id        → 用户 Open ID (ou_xxx)
reply_to_id    → 回复目标消息 ID (om_xxx)
is_group_chat  → boolean，直接从 runtime context 读取
```

路由规则：

```
群聊 → receive_id_type="chat_id", receive_id=chat_id, reply_to=reply_to_id
私聊 → receive_id_type="open_id", receive_id=open_id, reply_to=null
```

---

## 时序约束

1. **先通知再执行**：每个耗时操作之前调用 `send_user_notice()`。
2. **等待返回**：调用返回后才继续下一步。
3. **不累积**：检测到状态变化立即通知，不攒批。
4. **不重复**：message tool 成功后不再 fallback CLI。

---

## 失败处理

| 情况 | 行为 |
|------|------|
| 适配器返回失败 | 记录到 `notice_log`，继续主流程 |
| 凭据未配置 | stderr 警告，继续主流程 |
| 全部适配器不可用 | 退化为 assistant 文本 + 最终报告注明 |
| 严格模式（`--strict`） | 失败时中断（仅 CLI 调用方显式要求时） |

---

## Skill 引用方式

在 Skill 文件顶部 **执行检查清单** 区域添加一行引用即可：

```markdown
> **通知原语**：本 Skill 的所有 `send_user_notice()` 调用遵循 `6-skills/_shared/NOTICE-PRIMITIVE.md`，不在此重复适配器选择和调用格式。
```

Skill 中只需声明：

```markdown
| # | 时机 | 通知内容 |
|---|------|---------|
| 1 | Phase 1 开始 | "⏳ 正在扫描目录..." |
| 2 | Phase 2 完成 | "✅ 发现 N 个文件，开始预检..." |
```

调用格式、适配器选择、路由规则全部由本文件定义，Skill 不需要重复。

---

## 能力检测（可选脚本验证）

Agent 可在 session 启动时运行以下命令确认 CLI notify 可用：

```bash
python -m cli notify send --help | grep -q "receive-id-type" && echo "OK" || echo "UNAVAILABLE"
```

返回 `OK` 表示 CLI notify 已就绪，可作为 fallback 适配器。
