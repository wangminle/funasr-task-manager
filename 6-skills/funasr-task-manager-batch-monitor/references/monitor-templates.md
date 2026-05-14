# 子 Agent 监控通知模板

> 本文件定义 `funasr-task-manager-batch-monitor` Skill（子 Agent）在各阶段向用户发送的通知模板。
> 子 Agent 必须严格按模板拼接输出，不可自由组织回复内容。

> **发送方式（强制）**：以下所有模板内容必须通过 `send_user_notice()` 实时发送，禁止仅作为普通 assistant 文本输出。详见 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。
>
> **调用示例**：
>
> OpenClaw（首选 — 使用 `message` tool）：
> ```json
> {"name": "message", "arguments": {"action": "send", "message": "<模板渲染后的文本>"}}
> ```
>
> CLI fallback（无 message tool 时）：
> ```bash
> python -m cli notify send --text "<模板渲染后的文本>" --chat-id "<chat_id>"
> ```
> 私聊使用 `--receive-id-type open_id --chat-id "<open_id>"`。子 Agent 必须使用主 Agent 传入的 `notification_context`，不得依赖默认 `FEISHU_CHAT_ID`。

---

## 1. 启动确认通知（ack）

子 Agent 收到委托后 **5 秒内**必须发送此通知，作为向主 Agent 的启动确认信号。

**群聊模板**：

```
📊 批量转写监控已启动

批次：{batch_id}
会话：{chat_id}（群聊）
监控任务组：{group_count} 个（{group_ids_summary}）
文件总数：{total_files}
轮询间隔：{poll_interval_sec}s
超时上限：{timeout_human}
通知通道：{adapter}（message / cli-notify）

我将持续监控进度并向你汇报。
```

**私聊模板**：

```
📊 批量转写监控已启动

批次：{batch_id}
文件总数：{total_files}
预计 {timeout_human} 内完成
通知通道：{adapter}

持续监控中，进度变化时通知你。
```

`{group_ids_summary}`：如果 group 数量 ≤3，列出全部 ID（截取前 12 位）；否则显示 "首个: {first_id}... 共 {count} 个"。

`{timeout_human}`：秒数格式化，例如 "60m" 或 "1h"。

---

## 2. 进度更新通知

每次检测到 succeeded 或 failed 变化时发送。

**群聊模板**：

```
📊 批量转写进度

✅ 已完成：{succeeded}/{total}
❌ 失败：{failed}
⏳ 处理中：{in_progress}
⏱️ 已耗时：{elapsed_human}
🕐 预计剩余：{estimated_remaining_human}

批次：{batch_id}
```

**私聊模板**：

```
📊 {succeeded}/{total} 已完成，预计还需 {estimated_remaining_human}
```

`{estimated_remaining_human}`：基于已完成任务的平均耗时推算。如果尚无完成任务，显示 "估算中..."。

计算公式：

```
if succeeded > 0:
  avg_per_task = elapsed_sec / succeeded
  remaining = avg_per_task * (total - succeeded - failed)
else:
  remaining = "估算中..."
```

---

## 3. 心跳通知

无新变化超过 `heartbeat_interval_sec` 时发送：

```
💓 转写仍在运行

当前状态：{succeeded}/{total} 已完成，{in_progress} 个处理中
已耗时：{elapsed_human}
后端状态：正常

批次：{batch_id}
```

如果后端查询出现过警告（非致命），替换最后一行：

```
后端状态：最近 {warn_count} 次查询有延迟，但仍在响应
```

---

## 4. 异常通知

### 任务失败

检测到新的失败任务时发送（每个失败任务只通知一次）。**首次出现失败时，群聊 @ 触发用户**。

**群聊模板**（首次失败含 @ ）：

```
⚠️ 转写任务失败 <at user_id="{sender_id}">用户</at>

文件：{filename}
任务 ID：{task_id}
错误：{error_message}
批次：{batch_id}

当前进度：{succeeded}/{total} 已完成，{failed} 个失败
```

**私聊模板**（不 @）：

```
⚠️ 转写任务失败

文件：{filename}
错误：{error_message}
进度：{succeeded}/{total} 已完成，{failed} 个失败
```

### 后端不可达

连续查询失败时发送：

```
🚨 后端服务异常

连续 {fail_count} 次查询失败，最近错误：{last_error}
批次：{batch_id}
当前进度：{succeeded}/{total} 已完成

监控已暂停。后端恢复后可重新启动监控。
```

### 超时

```
⏰ 监控超时

已运行 {elapsed_human}，超过设定的 {timeout_human} 上限。
批次：{batch_id}
当前进度：{succeeded}/{total} 已完成，{failed} 个失败，{in_progress} 个仍在处理中

已完成的结果将自动下载。仍在运行的任务会继续执行，你可以稍后用以下命令查看：
python -m cli --output json task-group status {group_id}
```

---

## 5. 完成汇总通知

全部任务完成后发送：

**群聊模板 — 全部成功**（@ 触发用户）：

```
🎉 批量转写全部完成 <at user_id="{sender_id}">用户</at>

📁 批次：{batch_id}
📊 结果：{succeeded} 个全部成功
⏱️ 总耗时：{elapsed_human}
📂 结果目录：{output_dir}
📤 通知统计：{notice_sent} 条已送达，{notice_failed} 条未送达
```

**群聊模板 — 有失败项**（@ 触发用户）：

```
📋 批量转写已完成 <at user_id="{sender_id}">用户</at>

📁 批次：{batch_id}
📊 结果：{succeeded} 成功 / {failed} 失败 / {total} 总计
⏱️ 总耗时：{elapsed_human}
📂 结果目录：{output_dir}
📤 通知统计：{notice_sent} 条已送达，{notice_failed} 条未送达

失败文件：
{failed_list}

可以说"重试失败项"来重新处理。
```

**私聊模板 — 全部成功**（不 @）：

```
🎉 批量转写全部完成

{succeeded} 个文件全部成功
总耗时：{elapsed_human}
结果目录：{output_dir}
📤 通知统计：{notice_sent} 条已送达，{notice_failed} 条未送达
```

**私聊模板 — 有失败项**（不 @）：

```
📋 批量转写已完成

{succeeded} 成功 / {failed} 失败 / {total} 总计
总耗时：{elapsed_human}
结果目录：{output_dir}
📤 通知统计：{notice_sent} 条已送达，{notice_failed} 条未送达

失败文件：
{failed_list}

可以说"重试失败项"来重新处理。
```

`{failed_list}`：每个失败文件一行，格式为 `  - {filename}: {error_message}`。最多列出 10 个，超出时显示 `  - ...及其他 {remaining} 个`。

---

## 使用规则

1. 所有 `{变量}` 必须从 CLI 命令返回的 JSON 或启动参数中获取，不可由 Agent 推断或美化。
2. 不在模板外添加额外内容（如感谢语、性能分析表格等）。
3. 时间格式：秒数 < 120 用 "Xs"，120-3600 用 "Xm Ys"，>3600 用 "Xh Ym"。
4. 文件名超过 30 字符时截断为前 27 字符 + "..."。
5. **每条模板消息必须通过 `send_user_notice()` 发送**，不可依赖普通文本回复。
6. 发送失败时记录 warning，继续执行监控，在最终汇总中标注"N 条通知未送达"。
7. 子 Agent 全程不输出模板以外的对话内容。

---

## 群聊 vs 私聊模板差异

根据 `notification_context.is_group_chat` 选择模板变体。

### 群聊模板规则

- 异常通知和完成汇总中 **@ 触发用户**（通过飞书 `<at user_id="ou_xxx">用户名</at>` 语法）。
- 所有模板包含 `批次：{batch_id}`。
- 完成汇总中可追加 `群聊：{group_subject}`。

### 私聊模板规则

- **不 @ 用户**——私聊本身就是发给用户的。
- **不含群名、话题说明**。
- 进度通知可以更简洁：

```
📊 {succeeded}/{total} 已完成，预计还需 {estimated_remaining_human}
```

- 心跳通知可以更短：

```
💓 {succeeded}/{total} 已完成，仍在运行
```

- 完成汇总保持完整结构，但去掉 `群聊` 行和 @ mention。

### @ 触发规则（群聊专用）

| 通知类型 | 是否 @ |
|---------|--------|
| 启动确认 | 不 @ |
| 进度更新 | 不 @ |
| 心跳 | 不 @ |
| 首次失败 | **@ 触发用户一次** |
| 完成（有失败项） | **@ 触发用户** |
| 全部成功完成 | 可 @，消息简洁 |
