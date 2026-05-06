---
name: funasr-task-manager-result-delivery
description: >
  Monitor transcription tasks and deliver results with quality checks.
  Use when: task_id or task_group_id needs monitoring or result return,
  user asks to export txt/json/srt/zip, user requests quality check on
  transcription output, or results need re-delivery for an existing task.
---

# 结果交付与质量初筛

`funasr-task-manager-result-delivery` 是运行时闭环的出口 Skill。它负责在任务创建后监控状态、拉取结果、做基础质量检查，并把结果以合适形式返回到 channel。

## 执行检查清单（强制）

> **实时通知规范**：本 Skill 的所有用户通知必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。禁止用普通文本替代 `send_user_notice()`。

> **强制规则**：Agent 在执行结果交付流程时，**必须逐条通过 `send_user_notice()` 确认以下通知已送达**。即使使用 `background: true` 异步轮询，每次状态变化都必须调用 `send_user_notice()` 回报用户。

| # | 检查项 | 时机 | `send_user_notice()` 内容 |
|---|--------|------|--------------------------|
| 1 | 预处理状态 | Phase 2 | "⏳ {filename} — 文件预处理中..." |
| 2 | 排队状态 | Phase 2 | "⏳ {filename} — 等待调度..." |
| 3 | 转写状态 | Phase 2 | "⏳ {filename} — 正在转写..." |
| 4 | 分段进度 | Phase 2 | "⏳ {filename} — 转写中（{succeeded}/{total} 段已完成）" |
| 5 | 批量进度 | Phase 2 | "{completed}/{total} 已完成" |
| 6 | 完成/失败 | Phase 2 | "✅ 转写完成！" 或 "❌ 转写失败：{原因}" |
| 7 | 质量初筛 | Phase 4 | 异常时通知用户，正常时可跳过 |
| 8 | 结果交付 | Phase 5 | 固定模板 + 文件附件（见下方输出格式规范） |

#### `send_user_notice()` 调用方式

**OpenClaw 环境（首选）— 状态通知：**

```json
{"name": "message", "arguments": {"action": "send", "message": "⏳ tv-report-1.wav — 正在转写..."}}
```

**OpenClaw 环境 — 结果文件发送：**

```json
{"name": "message", "arguments": {"action": "send", "message": "✅ 转写完成\n\n  文件名: tv-report-1.wav\n  ...", "filePath": "/tmp/funasr-task-manager/results/tv-report-1.txt"}}
```

**CLI fallback（无 message tool 时）：**

```bash
python -m cli notify send --text "⏳ tv-report-1.wav — 正在转写..."
```

**时序要求**：轮询检测到状态变化后，**立即** `send_user_notice()` 发送通知，然后再继续下一轮轮询。不要累积多个状态变化再一起发送。

### 常见执行偏差（必读）

| 偏差模式 | 典型表现 | 正确做法 |
|---------|---------|---------|
| **只发结果不发进度** | 等任务完成后才发第一条消息 | 每次状态变化调用 `send_user_notice()` |
| **用普通文本代替** | 输出 assistant 文本但未调用 message tool | 必须调用 `send_user_notice()` 而非普通文本 |
| **轮询不回报** | 后台轮询 API 但不通知用户 | 每次检测到状态变化立即 `send_user_notice()` |
| **批量沉默** | 批量任务全部完成才汇报 | 每完成一个文件就 `send_user_notice()` 更新进度 |
| **分段细节过多** | 在最终结果里展示段级 JSON | 进度中简报"3/5 段已完成"，最终结果不含段级细节 |
| **模板外内容** | 添加性能表格、加速比等 | 严格按模板输出，不增不减 |
| **重复发送** | message tool 和 CLI 都调用了 | 只使用第一个可用方式，成功后不 fallback |

### 任务完成后自检（强制）

Agent 在完成每一批任务交付后，必须进行以下自检：

```
自检清单：
- [ ] 我是否在每个状态变化时都通过 send_user_notice() 发送了通知？
- [ ] 用户是否始终知道当前进度（而非 turn 结束后才收到）？
- [ ] 大文件/长时间操作是否有额外的进度通知？
- [ ] 结果是否严格按模板发送（无额外内容）？
- [ ] txt 文件是否以附件形式通过 send_user_notice(filePath=...) 发送？
```

> **2026-04-28 复盘教训**：OpenClaw 机器人在 6 个文件的转写流程中，全程未发送任何阶段通知，用户长时间等待无反馈。
> **2026-05-05 排查结论**：Agent 有 `message` tool 但只在发结果文件时调用了一次，中间进度全部靠普通文本被 turn 缓冲。修复：**每次状态变化必须显式调用 `send_user_notice()`**。

## 触发条件

### 自动触发

- `funasr-task-manager-channel-intake` 成功创建任务后，传入 `task_id(s)` 或 `task_group_id`
- 批量任务提交成功后，需要持续回报完成进度

### 用户显式触发

- 用户说"把这个任务结果发我""重新导出字幕""下载 json""检查这批结果质量"
- 用户提供 `task_id` 或 `task_group_id`

### 关键词

`结果` / `导出` / `下载` / `字幕` / `srt` / `json` / `txt` / `质量` / `乱码` / `空文本` / `result` / `export` / `download`

### 不触发

- 新文件上传请求（→ `funasr-task-manager-channel-intake`）
- benchmark 请求（→ `funasr-task-manager-server-benchmark`）
- 普通服务器注册请求

## 执行流程

### Phase 1：接收任务上下文

- 输入：`task_id(s)` 或 `task_group_id`
- 输入：期望格式 `txt` / `json` / `srt` / `zip`
- 输入：channel 回传能力（可发文本/可发文件/大小限制）
- 输入：原始文件名（用于结果文件命名）
- 缺少任务标识 → 向用户询问

### Phase 2：监控任务状态

- 单任务：`GET /api/v1/tasks/{task_id}`
- 批量：`GET /api/v1/task-groups/{group_id}`
- 可选：`GET /api/v1/tasks/{task_id}/progress`（SSE 实时进度）
- 状态变化时回报关键节点：
  - `PREPROCESSING` → "文件预处理中..."（长音频会自动 VAD 切分）
  - `QUEUED` → "等待调度..."
  - `TRANSCRIBING` → "正在转写..."
  - `SUCCEEDED` → "转写完成！"
  - `FAILED` → "转写失败：{原因}"
- 分段任务：响应中包含 `segments` 字段（`total`/`succeeded`/`failed`/`pending`/`active`），可用于汇报 "3/5 段已完成"
- 批量任务：定期汇报 "{completed}/{total} 已完成"
- 超时后给出当前状态和下一步建议，不盲目取消任务

### Phase 3：拉取结果

- 单任务：`GET /api/v1/tasks/{task_id}/result?format=txt`（`format` 参数为 `json` | `txt` | `srt` 三选一）
- 批量：`GET /api/v1/task-groups/{group_id}/results?format=zip`（`format` 参数为 `json` | `txt` | `srt` | `zip` 四选一）
- 无成功任务 → 返回失败摘要，不假装成功

### Phase 4：质量初筛

- **空文本** → 标记异常，建议检查音频是否静音或语言/模型是否匹配
- **明显乱码** → 标记异常，建议检查编码、音频质量或输入格式
- **文本过短** → 提醒可能是静音、噪声或截断
- **批量任务** → 汇总成功/失败/空文本数量
- **正常** → 进入交付

### Phase 5：结果交付

**核心原则**：所有结果以 **txt 文件** 形式发送到原渠道，不在消息中引用转写全文。txt 文件名与用户发送的原始文件名一致（仅替换扩展名为 `.txt`）。

#### 交付步骤

1. **生成结果文件**
   - 文件名规则：`{原始文件名去扩展名}.txt`
   - 示例：用户发送 `会议录音-20260415.mp4` → 结果文件名为 `会议录音-20260415.txt`
   - 批量任务：每个文件各生成一个同名 txt
   - **保存路径规范**：
     - **禁止**将结果文件直接写入 workspace/项目根目录
     - 必须保存到 Skill 专属临时目录：`{TMPDIR}/funasr-task-manager/{task_group_id}/`
     - 如果用户指定了输出目录，使用用户指定的路径
     - 示例：`/tmp/funasr-task-manager/01KQ8QER.../会议录音-20260415.txt`
     - 文件发送到渠道后，本地临时副本可以保留（方便重发）或由用户手动清理

2. **发送结构化摘要消息**（固定格式，见下方模板）

3. **通过 `send_user_notice()` 发送结果文件**（不是贴文本到消息框）
   - 以**文件附件**形式发送到原渠道
   - 批量任务：逐个发送或打包为 zip
   - **如果渠道不支持文件附件**：将 txt 内容保存到服务器，发送下载链接

   **`send_user_notice()` 文件发送方式（按优先级选择）**：

   **优先级 1：OpenClaw `message` tool（首选）**
   ```json
   {"name": "message", "arguments": {"action": "send", "message": "✅ 转写完成\n\n  文件: 会议录音-20260415.txt", "filePath": "/tmp/funasr-task-manager/01KQ8QER.../会议录音-20260415.txt"}}
   ```
   成功判断：toolResult 中 `ok == true`。

   **优先级 2：CLI `notify send-file`（无 message tool 时）**
   ```bash
   python -m cli notify send-file --file "/tmp/funasr-task-manager/01KQ8QER.../会议录音-20260415.txt" --text "✅ 转写完成 — 会议录音-20260415.txt"
   ```
   上述命令依赖 `FEISHU_CHAT_ID` 或 `notify.default_chat_id` 已配置；如果没有默认会话 ID，必须显式传入 `--chat-id "oc_xxx"`。

   显式指定会话：
   ```bash
   python -m cli notify send-file --file "result.txt" --text "✅ 转写完成" --chat-id "oc_xxx"
   ```

   带回复线程：
   ```bash
   python -m cli notify send-file --file "result.txt" --text "✅ 转写完成" --reply-to "om_xxx"
   ```

   **各渠道底层 API 参考**：

   | 渠道 | 发送方式 | 关键 API |
   |------|---------|---------|
   | **飞书** | 先上传文件获取 `file_key`，再发 `file` 类型消息 | `POST /open-apis/im/v1/files` → `POST /open-apis/im/v1/messages?receive_id_type=chat_id`（msg_type=file）⚠️ URL 必须带 `receive_id_type` |
   | **企业微信** | 先上传临时素材获取 `media_id`，再发文件消息 | `POST /cgi-bin/media/upload` → `POST /cgi-bin/message/send`（msgtype=file） |
   | **Slack** | 使用 `files.uploadV2` 直接上传并发送 | `POST /api/files.uploadV2` |
   | **Discord** | 在发送消息时附带文件 | `POST /api/v10/channels/{id}/messages`（multipart） |
   | **CLI / 本地** | 写入本地文件路径 | 直接 `cp` 到用户指定目录 |

   详细 API 参考见 `funasr-task-manager-channel-intake` Skill 中的 `references/channel-file-apis.md`（与本 Skill 同级目录下的兄弟 Skill）。

   > **注意**：使用 `message` tool 成功后禁止再 fallback 到 CLI `notify send-file`（避免重复发送）。

4. **不做的事**
   - **不在消息中引用/粘贴转写全文**（无论长短，这是最常被违反的规则）
   - 不让大模型自由组织回复内容
   - 不生成摘要代替全文
   - 不把结果文本作为消息体发送（应使用文件附件 API）

## 输出格式规范（强制模板）

> **强制执行**：以下模板是硬编码格式，Agent 必须逐字段拼接输出，**禁止**自由组织回复、添加性能对比表格、段级调度详情或任何模板外内容。如果 Agent 输出了模板中没有定义的字段或格式（如 Markdown 表格、JSON 代码块、加速比等），视为违反本规范。
>
> **核心规则**：
> 1. 只输出模板中定义的字段，不增不减
> 2. 字段值全部来自 API 返回，不做推断或美化
> 3. 转写全文以 txt 文件附件发送，不粘贴到消息中
> 4. 分段信息（如有）仅在进度通知中简要体现（如"3/5 段已完成"），不在最终结果中展示段级细节

### 单任务成功

Agent 必须严格按以下模板拼接发送，**所有字段来自 API 返回值和任务上下文**，不由大模型自由生成：

```
✅ 转写完成

  文件名:   {original_filename}
  音频时长: {duration_human}（{duration_sec}s）
  音频格式: {file_format}
  转写耗时: {elapsed_sec}s（RTF: {rtf}）
  文本长度: {text_length} 字
  结果文件: {result_filename}

  📎 结果文件已发送，请查收。
```

**字段来源**：

| 字段 | 来源 |
|------|------|
| `original_filename` | intake 阶段记录的用户原始文件名 |
| `duration_human` | 从 `duration_sec` 计算：`{h}h {m}m {s}s`（去掉 0 值段） |
| `duration_sec` | `task.file.duration_sec` 或 preflight 阶段的值 |
| `file_format` | 原始文件扩展名大写，如 `MP4`、`WAV`、`MP3` |
| `elapsed_sec` | `completed_at - started_at`，取整 |
| `rtf` | `elapsed_sec / duration_sec`，保留 2 位小数 |
| `text_length` | `len(result_txt)`（去除首尾空白后） |
| `result_filename` | `{original_filename_stem}.txt` |

### 批量任务成功

```
✅ 批量转写完成

  批次 ID:  {task_group_id}
  文件数量: {total} 个
  成功:     {succeeded}/{total}
  失败:     {failed}/{total}
  总耗时:   {total_elapsed}s

  文件明细:
  ┌──────────────────────────┬─────────┬─────────┬──────────┐
  │ 文件名                   │ 时长    │ 耗时    │ 状态     │
  ├──────────────────────────┼─────────┼─────────┼──────────┤
  │ {filename_1}             │ {dur_1} │ {ela_1} │ ✅ 成功  │
  │ {filename_2}             │ {dur_2} │ {ela_2} │ ✅ 成功  │
  │ {filename_3}             │ {dur_3} │ -       │ ❌ 失败  │
  └──────────────────────────┴─────────┴─────────┴──────────┘

  📎 成功文件的转写结果已逐个发送，请查收。
```

### 任务失败

```
❌ 转写失败

  文件名: {original_filename}
  任务 ID: {task_id}
  失败原因: {error_message}

  建议:
  - {suggestion}
```

### 进度通知（状态变化时）

```
⏳ {original_filename} — {status_description}
```

仅在状态发生变化时发送，不重复发送相同状态。

## 文件命名规则

| 场景 | 命名规则 | 示例 |
|------|---------|------|
| 单文件 | `{stem}.txt` | `会议录音.mp4` → `会议录音.txt` |
| 批量同名 | `{stem}_{序号}.txt` | 两个 `录音.wav` → `录音.txt`、`录音_1.txt` |
| SRT 格式 | `{stem}.srt` | `采访.mp3` → `采访.srt` |
| JSON 格式 | `{stem}.json` | `演讲.wav` → `演讲.json` |
| 批量打包 | `batch_{group_id}.zip` | `batch_01JA...zip` |

## 失败处理规范

| 场景 | Agent 应做的事 | 不应做的事 |
|------|--------------|----------|
| 任务仍在运行 | 告知当前状态和预计等待方式 | 重复创建任务 |
| 任务失败 | 返回 `error_message`，并建议是否重试 | 隐藏失败原因 |
| 没有成功任务 | 返回批次失败摘要 | 返回空 zip 当作成功 |
| 文本为空 | 标记质量异常 | 直接说"转写成功" |
| 用户要求安全返回 | 按安全模式 fallback 处理（见下方说明） | 直接明文贴回 channel |
| 用户要求重新导出 | 重新拉取并按新格式导出 | 拒绝或重新创建任务 |

## 安全模式交付 Fallback

`funasr-task-manager-secure-ingest` **当前尚未创建**（P2）。在此 Skill 创建之前，结果交付阶段遇到安全/敏感场景时，Agent **不可假装具备加密返回能力**。

**当前阶段的处理规则：**

1. 如果任务在创建时已被 `channel-intake` 标记为敏感/加密场景：
   - 结果交付前**必须再次确认**用户是否接受明文返回
   - 明确告知："当前系统尚未支持加密结果输出。转写结果将以明文形式返回，请确认是否继续。"
   - 用户确认 → 以明文方式交付，在结果中标注"⚠ 以明文返回，请注意信息安全"
   - 用户拒绝 → 建议通过 CLI 在本地获取结果：`python -m cli task result <task_id> --format txt --save ./private-results/result.txt`
2. 如果用户在结果交付阶段首次提出保密/加密要求：
   - 告知："当前系统不支持加密结果输出。"
   - 提供替代方案（同上 CLI 本地下载）
3. **严禁**：
   - 静默以明文返回敏感任务结果
   - 假装结果已加密
   - 将转写全文写入日志

**未来 `funasr-task-manager-secure-ingest` 上线后**，本 Skill 应将安全模式结果交付切换到该规程处理。

## 结果格式说明

| 格式 | 端点参数 | 内容 |
|------|---------|------|
| `txt` | `?format=txt` | 纯文本转写结果 |
| `json` | `?format=json` | JSON 结构（含时间戳、置信度等元数据） |
| `srt` | `?format=srt` | SRT 字幕格式（含时间轴） |
| `zip` | `?format=zip`（仅批量） | 打包所有任务的结果文件 |

## 与其他 Skill 的协作

| 协作场景 | Skill | 状态 | 说明 |
|---------|-------|------|------|
| 接收任务上下文 | `funasr-task-manager-channel-intake` | ✅ 可用 | intake 在 Phase 5 交接 task_id(s)、原始文件名和用户偏好 |
| 安全模式结果 | `funasr-task-manager-secure-ingest` | ⏳ 未创建 | **当前 fallback**：见上方安全模式交付说明 |

## 相关文件

- `references/result-formats.md`：txt/json/srt/zip 导出规则
- `references/quality-checklist.md`：空文本、乱码、异常短文本检查规则
- `references/response-templates.md`：结果回传模板
