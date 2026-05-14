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

## 适配器选择（按优先级，含路由验证）

### 优先级 1：平台 `message` tool（需通过路由验证）

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

**⚠ 路由验证（强制）：** 每条通知发送后，必须检查 `toolResult.chatId` 是否匹配预期目标（见下方"路由验证机制"）。

### 优先级 2：CLI notify（Agent 显式路由）

条件：无 `message` tool，或 `message` tool 路由验证失败（见下方降级规则），或 `message` tool 连续失败且已配置飞书凭据。

**发送文本（Agent/Skill 必须携带显式路由参数）：**

```bash
# 私聊
python -m cli notify send --text "<text>" --receive-id-type open_id --chat-id <open_id>
# 群聊
python -m cli notify send --text "<text>" --chat-id <chat_id>
# 群聊 + 线程回复
python -m cli notify send --text "<text>" --chat-id <chat_id> --reply-to <reply_to_id>
```

**发送文件附件：**

```bash
python -m cli notify send-file --file "<filePath>" --filename "<display_name>" --receive-id-type <type> --chat-id <id>
```

**成功判断：** exit 0 且 stdout 输出 `message_id=om_xxx`

**重要**：CLI 本身支持 `FEISHU_CHAT_ID` / `notify.default_chat_id` 作为本地手动调试的默认目标；但 Agent/Skill 调用 `send_user_notice()` 时必须始终携带显式 `--chat-id`，私聊还必须携带 `--receive-id-type open_id`。默认目标可能残留上一个 session 的会话，不能用于自动化通知。

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

## 路由验证机制（强制）

> **背景**：`message` tool 的路由由平台上下文决定，Agent 无法指定目标。平台上下文在 session 切换时可能存在传播延迟，导致消息被投递到上一个 session 的会话（如：私聊发起的任务，消息却发到了群聊）。

### 预期目标计算

Agent 在提取 `notification_context` 后，计算当前 session 的**预期投递目标**：

```
if is_group_chat:
    expected_chat_id = chat_id          # oc_xxx
else:
    expected_chat_id = null             # 私聊时无法预知 p2p 会话 ID，需通过前缀排除法验证
```

### 每条通知的验证流程

```
send_user_notice(text):
    if route_locked_to_cli:
        → 直接走 CLI notify（显式路由），跳过 message tool
    
    result = message_tool(text)
    
    if result.ok:
        actual_chat_id = result.chatId
        
        # --- 群聊验证：精确匹配 ---
        if is_group_chat and actual_chat_id == expected_chat_id:
            → 路由正确，记录成功
        
        # --- 私聊验证：前缀排除 + 一致性检查 ---
        elif not is_group_chat:
            if actual_chat_id starts with "oc_":
                → 路由异常！私聊消息被发到了群聊。执行降级（见下方）
            elif verified_p2p_chat_id is null:
                → 首次验证通过，记录 verified_p2p_chat_id = actual_chat_id
            elif actual_chat_id == verified_p2p_chat_id:
                → 路由一致，记录成功
            else:
                → 路由漂移！chatId 与首次验证值不同。执行降级（见下方）
        
        # --- 其他不匹配场景 ---
        else:
            → 路由异常！执行降级
        
        # --- 降级动作 ---
        降级:
            1. 标记 route_locked_to_cli = true
            2. 立即通过 CLI notify 向正确目标重发同一条消息
               （私聊：--receive-id-type open_id --chat-id <open_id>）
               （群聊：--chat-id <chat_id> [--reply-to <reply_to_id>]）
            3. 记录 notice_log: {type: "route_mismatch", expected: ..., actual: ...}
    else:
        → message tool 失败，降级到 CLI notify
```

> **关键设计**：私聊场景无法预知 `message` tool 返回的 p2p 会话 ID 格式（不一定等于 `open_id`），因此不做精确匹配。但可以确定一点：**返回的 `chatId` 以 `oc_` 开头一定是群聊 ID，私聊上下文不应出现**。这是检测"私聊消息发到群聊"这一核心故障场景的关键判据。

### 降级规则

| 触发条件 | 动作 | 后续影响 |
|---------|------|---------|
| 群聊：`chatId` ≠ `expected_chat_id` | 锁定 CLI 路由 + 重发到正确目标 | 本 session 所有后续通知均走 CLI |
| 私聊：`chatId` 以 `oc_` 开头（群聊 ID 泄入私聊上下文） | 锁定 CLI 路由 + 用 `open_id` 重发 | 本 session 所有后续通知均走 CLI |
| 私聊：`chatId` 与 `verified_p2p_chat_id` 不一致 | 锁定 CLI 路由 + 用 `open_id` 重发 | 本 session 所有后续通知均走 CLI |
| `message` tool 连续 2 次失败 | 切换到 CLI notify | 本 session 所有后续通知均走 CLI |
| CLI notify 也失败 | 退化为 assistant 文本 | 最终报告注明 |

### Session 级状态变量

Agent 在 session 生命周期内维护以下路由状态：

```
route_locked_to_cli: bool = false       # 是否已锁定到 CLI 路由
verified_p2p_chat_id: string | null     # 私聊场景下首次验证通过的 chatId
route_mismatch_count: int = 0           # 路由不匹配次数
```

**`route_locked_to_cli` 一旦设为 true，本 session 内不可回退。**

---

## 时序约束

1. **先通知再执行**：每个耗时操作之前调用 `send_user_notice()`。
2. **等待返回**：调用返回后才继续下一步。
3. **不累积**：检测到状态变化立即通知，不攒批。
4. **路由验证不可跳过**：每条通过 `message` tool 发出的通知都必须验证 `chatId`。
5. **降级不可逆**：一旦检测到路由错误并锁定到 CLI，本 session 不再尝试 `message` tool。

---

## 失败处理

| 情况 | 行为 |
|------|------|
| 适配器返回失败 | 记录到 `notice_log`，继续主流程 |
| 路由验证失败（chatId 不匹配） | 锁定 CLI + 重发到正确目标，记录到 `notice_log` |
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
