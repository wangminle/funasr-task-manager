---
name: funasr-task-manager-channel-intake
description: >
  Recognize transcription intent and orchestrate task submission when audio/video
  files or ASR keywords appear in a channel. Use when: audio/video files arrive,
  user mentions transcription/ASR/subtitle, user asks to batch-process media,
  or agent needs to proactively guide a user into the transcription pipeline.
---

# 音频入口与意图编排

这是 funasr-task-manager Skill 体系中最核心的入口。它让 Agent 从"被动等命令"变成"看到文件后主动引导用户完成转写"。

职责：**识别用户意图并完成最小必要的入口编排**——判断是否启动 ASR、完成参数协商、执行预检查、提交任务，并把后续监控与结果交付交给 `funasr-task-manager-result-delivery`。

> MVP 阶段可把进度监控和结果交付临时写在此 Skill 中，但文档和实现都应保留 `funasr-task-manager-result-delivery` 的拆分边界，避免入口 Skill 演化成巨型 Skill。

## 执行检查清单（强制）

> **实时通知规范**：本 Skill 的所有用户通知必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。禁止用普通文本替代 `send_user_notice()`。

> **强制规则**：Agent 在执行本 Skill 流程时，**必须逐条确认以下通知已通过 `send_user_notice()` 发送**。跳过任何一项需要明确理由（如用户明确说"不用通知"）。使用 `background: true` 执行异步操作时，**更需要主动调用 `send_user_notice()` 回报状态**，不可静默等待结果。

| # | 检查项 | 时机 | `send_user_notice()` 内容 | 发送后再执行 |
|---|--------|------|--------------------------|-------------|
| 1 | 收到文件确认 | Phase 1 | "收到 N 个音频/视频文件：{文件列表}" | 意图判断 |
| 2 | 意图确认 | Phase 1 | "需要转写吗？" 或直接进入（高置信度） | — |
| 3 | 开始下载通知 | Phase 1.5 | "⏳ 正在从{渠道}下载文件..." | curl 下载 |
| 4 | 下载完成通知 | Phase 1.5 | "✅ 下载完成：{N}/{total} 个文件（{size}MB）" | ffprobe 预检 |
| 5 | 预检查结果 | Phase 2 | 异常时通知，正常时可跳过 | 上传文件 |
| 6 | 开始上传通知 | Phase 4 | "⏳ 正在上传到转写引擎..." | POST /files/upload |
| 7 | 任务提交确认 | Phase 4 | "✅ 已提交 N 个文件，任务编号 {task_id}，预计 {eta} 完成" | 移交监控 |
| 8 | 移交结果交付 | Phase 5 | 无需用户可见通知，内部交接 | — |

#### `send_user_notice()` 调用方式

**OpenClaw 环境（首选）：**

```json
{"name": "message", "arguments": {"action": "send", "message": "收到 1 个音频文件：tv-report-1.wav"}}
```

**CLI fallback（无 message tool 时）：**

```bash
python -m cli notify send --text "收到 1 个音频文件：tv-report-1.wav"
```

**时序要求**：每条通知必须在对应耗时操作**之前**发送，等待 toolResult 返回 `ok: true` 后再执行下一步。

### 常见执行偏差（必读）

| 偏差模式 | 典型表现 | 正确做法 |
|---------|---------|---------|
| **静默执行** | 全程不发送任何状态通知，直到最终结果 | 每个 Phase 至少一次 `send_user_notice()` |
| **用普通文本代替** | 输出 assistant 文本"正在下载..."但未调用 message tool | 必须调用 `send_user_notice()` 而非普通文本 |
| **效率优先跳过** | 觉得"快干快完"，中间通知打断节奏 | 通知是用户体验核心，不是可选项 |
| **后台遗忘** | `background: true` 后忘记回报状态 | 异步操作完成后立即 `send_user_notice()` |
| **大文件焦虑** | 处理大文件时把精力放在技术问题上 | 大文件/长时间操作**更需要**频繁通知 |
| **只发结果** | 只在最后发送汇总和文件 | 各阶段都要 `send_user_notice()`，用户需要知道进度 |
| **重复发送** | message tool 和 CLI 都调用了 | 只使用第一个可用方式，成功后不 fallback |

> **2026-04-28 复盘教训**：OpenClaw 机器人实际执行中，9 个通知阶段全部跳过，只做了最终交付。根因是"效率优先心态"和"无执行检查机制"。
> **2026-05-05 排查结论**：批量转写 session 中 Agent 有 `message` tool 可用但未调用，所有阶段文本通过 OpenClaw 飞书 bridge 在 turn 结束后统一推送。本清单要求**必须显式调用 `send_user_notice()` 而非依赖普通文本**。

## 触发条件

### 文件触发

channel 中出现音视频文件（扩展名匹配以下列表）：

`.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm`

### 关键词触发

用户消息包含以下关键词（完整列表见 `references/trigger-keywords.json`）：

- 中文：`转写` / `识别` / `字幕` / `转文字` / `语音转文本` / `批量任务`
- 英文：`ASR` / `FunASR` / `transcribe` / `subtitle`
- Profile：`smoke` / `remote-standard` / `standard` / `full`

### 组合触发

- 文件 + 任何文字说明（即便不含关键词，有文件就应主动询问）
- 引用/转发了包含音视频的消息

### 不触发

- 纯文本闲聊
- 图片文件（`.jpg` / `.png` / `.gif` 等）
- 文档文件（`.pdf` / `.docx` / `.xlsx` 等），除非用户明确提及转写

## 三层感知模型

```
Layer A：感知（Perception）
  ├─ channel 中有新内容吗？
  ├─ 内容包含文件吗？什么类型？
  └─ 内容包含文字吗？什么语义？

Layer B：识别（Recognition）
  ├─ 这是 ASR 任务请求吗？
  ├─ 置信度如何？
  │   ├─ 高（有文件 + 明确关键词）→ 直接进入参数收集
  │   ├─ 中（有文件无关键词）     → 主动询问 "需要转写吗？"
  │   └─ 低（有关键词无文件）     → 提示 "请发送音频/视频文件"
  └─ 用户提到敏感/保密？ → 进入安全 fallback（当前：拒绝假装加密，提示未支持加密管道；未来：切换到 funasr-task-manager-secure-ingest）

Layer C：决策（Decision）
  ├─ 单文件还是批量？
  ├─ 需要什么输出格式？
  ├─ 需要先检查服务器状态吗？
  └─ 可以直接提交还是需要更多参数？
```

## 完整交互流程

### Phase 1：意图确认

- 识别文件和任务意图
- 高置信度 → 跳过确认，直接到 Phase 1.5
- 中/低置信度 → 主动询问用户
- 超时无响应 → 不执行，记录日志

### Phase 1.5：渠道文件获取

> **为什么需要这一步**：当 Agent 运行在聊天平台（飞书、企业微信、Slack 等）中时，用户发送的文件存储在平台服务器上，Agent 必须先通过平台 API 下载到本地才能上传到 ASR 后端。这一步是耗时瓶颈——如果 Agent 不知道怎么鉴权和下载，会花几分钟探索各种路径。

#### Step 1：判断文件来源

- **本地文件**（CLI / 本地 Agent）→ 跳过本阶段，直接到 Phase 2
- **渠道消息文件**（飞书 / 企业微信 / Slack / Discord 等）→ 继续

#### Step 2：渠道鉴权

**优先使用预配置凭据**，避免运行时探索。凭据应在 `funasr-task-manager-init` Phase 7 中配置好，存放在 Agent 平台的凭据存储中。

| 渠道 | 鉴权方式 | 凭据来源 |
|------|---------|---------|
| **飞书/Lark** | `app_id` + `app_secret` → `POST /open-apis/auth/v3/tenant_access_token/internal` → `tenant_access_token` | Agent 配置文件或环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |
| **企业微信** | `corpid` + `corpsecret` → `GET /cgi-bin/gettoken` → `access_token` | 环境变量 `WECOM_CORP_ID` / `WECOM_CORP_SECRET` |
| **Slack** | Bot OAuth Token（`xoxb-...`） | 环境变量 `SLACK_BOT_TOKEN` |
| **Discord** | Bot Token | 环境变量 `DISCORD_BOT_TOKEN` |

**关键规则**：
- **不要在运行时搜索凭据**（如 grep JS 源码、find 配置文件），这是上次 8 分钟延迟的主因
- 如果凭据不可用 → 立即告知用户："缺少飞书/微信凭据，请先配置"，引导进入 `init` Phase 7
- Token 应缓存，避免每次任务都重新获取（飞书 `tenant_access_token` 有效期 2 小时）

#### Step 3：下载文件到本地

先确定 Skill 专属临时目录（跨平台）：

```bash
# Linux / macOS
TMPDIR="${TMPDIR:-/tmp}"
WORK_DIR="$TMPDIR/funasr-task-manager"
mkdir -p "$WORK_DIR"

# Windows PowerShell
# $WORK_DIR = Join-Path $env:TEMP "funasr-task-manager"
# New-Item -ItemType Directory -Force -Path $WORK_DIR
```

> **⚠️ 禁止将下载文件或转写结果直接存放在 workspace/项目根目录**。所有中间文件必须保存到 `$WORK_DIR`（即 `{TMPDIR}/funasr-task-manager/`）下。如果用户指定了输出目录，使用用户指定的路径。

**飞书**：

> **⚠️ 不要使用 drive API**：`/open-apis/drive/v1/files/{file_key}/download` 是云文档/网盘接口，需要 `drive:file:readonly` 权限，对**聊天中发送的文件附件无效**（返回 403 / 错误码 99991672）。对话文件必须用下方的 `im/v1/messages` 接口。

```bash
curl -s -o "$WORK_DIR/{original_filename}" \
  -H "Authorization: Bearer {tenant_access_token}" \
  "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file"
```

**飞书大文件回退**（>50MB 会返回错误码 `234037: Downloaded file size exceeds limit`）：

```bash
# 使用 HTTP Range 分块下载（10MB/块）
CHUNK_SIZE=$((10 * 1024 * 1024))
OFFSET=0
OUTFILE="$WORK_DIR/{original_filename}"

# 第一次请求获取总大小
TOTAL_SIZE=$(curl -sI -H "Authorization: Bearer {tenant_access_token}" \
  -H "Range: bytes=0-0" \
  "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file" \
  | grep -i content-range | sed 's|.*/||; s|[^0-9]||g')

> "$OUTFILE"
while [ $OFFSET -lt $TOTAL_SIZE ]; do
  END=$(( OFFSET + CHUNK_SIZE - 1 ))
  [ $END -ge $TOTAL_SIZE ] && END=$(( TOTAL_SIZE - 1 ))
  curl -s -H "Authorization: Bearer {tenant_access_token}" \
    -H "Range: bytes=${OFFSET}-${END}" \
    "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file" \
    >> "$OUTFILE"
  OFFSET=$(( END + 1 ))
done
```

> **判断逻辑**：先尝试直接下载，若返回 `234037` 错误码则自动切换到 Range 分块下载。Agent 应在下载大文件时通知用户："⏳ 文件较大（{size}MB），使用分块下载..."

**企业微信**：

```bash
curl -s -o "$WORK_DIR/{original_filename}" \
  "https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token={access_token}&media_id={media_id}"
```

**Slack**：

```bash
curl -s -o "$WORK_DIR/{original_filename}" \
  -H "Authorization: Bearer {bot_token}" \
  "{url_private_download}"
```

**批量文件处理**：
- 多个文件应**并行下载**（`asyncio.gather` 或后台子进程），不要串行等待
- 下载进度通知用户："正在从飞书下载 3 个文件..."
- 下载完成 → 记录本地路径列表，进入 Phase 2

#### Step 4：下载验证

- 验证文件大小 > 0
- 验证文件扩展名匹配
- 下载失败 → 报告具体文件和错误原因，不静默跳过

详细的渠道 API 参考见 `references/channel-file-apis.md`。

### Phase 2：预检查

- 进入 `funasr-task-manager-media-preflight` 规程执行预检查（文件格式、大小、时长、转码需求）
- 检查 ffmpeg/ffprobe 可用性
- 检查后端服务健康：`GET /health` → `status: "ok"`
- 检查可用服务器：`GET /api/v1/stats` → `server_online > 0`

  **权限说明**：`GET /api/v1/servers` 依赖 AdminUser 认证，普通 API Key 会返回 403。运行时可用性判断应使用无需 admin 权限的 `/api/v1/stats`（返回 `server_online`/`server_total`）。如需获取完整服务器列表（如排查调度问题），Agent 必须持有 admin token。

- 预检查失败 → 明确告知用户哪个环节有问题

### Phase 3：参数协商

- 询问输出格式（有默认值，非强制交互）
  - 默认：`txt` + `json`，用户可选 `srt`
- 判断单文件 / 批量任务
  - 1 个文件 → 单文件流程
  - 多个文件 → 批量流程，自动生成 `task_group_id`
- 特殊参数（如有）：语言、热词列表、回调 URL
- 长音频切分：用户可选 `segment_level`（off/10m/20m/30m，默认 10m）
- 如果用户说"默认就行" → 全部使用默认值

### Phase 4：任务提交

- 上传文件：`POST /api/v1/files/upload` → 记录 `file_id`
- 创建任务：`POST /api/v1/tasks`

  ```json
  {
    "items": [
      {"file_id": "...", "language": "zh", "options": {...}}
    ],
    "callback": {"url": "...", "secret": "..."},
    "segment_level": "10m"
  }
  ```

  → 记录 `task_id(s)` 和 `task_group_id`

  **分段参数协商**：
  - `segment_level`（默认 `10m`）：切分策略。`off` 关闭切分；`10m`/`20m`/`30m` 按时长阈值自动决定是否切分。用户说"不要切分"时传 `off`；用户偏好较少切分时可选 `20m` 或 `30m`
  - 如用户未提及，使用默认值即可
- 向用户确认："已提交 N 个文件，任务编号 xxx"
- 提交失败 → 报告错误原因

### Phase 5：移交结果交付

- 将上下文交接给 `funasr-task-manager-result-delivery` 的执行流程
- 交接字段：`task_id(s)`、`task_group_id`、用户偏好格式、channel 回传方式
- 若 `funasr-task-manager-result-delivery` 尚未创建，可临时在本 Skill 内执行下方临时流程

### 临时内置的结果交付流程（未来拆入 `funasr-task-manager-result-delivery`）

**Phase A：进度监控**

- 轮询任务状态（5-10 秒间隔）：`GET /api/v1/tasks/{task_id}`
- 或通过 SSE 监听：`GET /api/v1/tasks/{task_id}/progress`
- 状态变化时通知用户（仅变化时发送一次）：`⏳ {原始文件名} — {状态描述}`
- 批量任务：汇报完成进度 "3/5 已完成"
- 超时策略：
  - 单文件：5 分钟超时
  - 批量 ≤ 5 个：20 分钟超时
  - 批量 > 5 个：按文件数 × 5 分钟

**Phase B：结果交付**

- 拉取结果
  - 单任务：`GET /api/v1/tasks/{task_id}/result?format=txt`
  - 批量：`GET /api/v1/task-groups/{group_id}/results?format=zip`
- 结果质量初筛
  - 空文本 → 标记异常，建议重试或检查音频质量
  - 明显乱码 → 标记异常
  - 正常 → 继续交付
- 向用户返回（**严格按 `funasr-task-manager-result-delivery` 的强制模板**，禁止自由组织回复）
  - 发送固定格式摘要消息（仅包含：文件名、时长、格式、转写耗时、文本长度、结果文件名）
  - 以 **txt 文件附件** 形式发回原渠道，文件名与用户发送的原始文件名一致（仅换扩展名为 `.txt`）
  - **不在消息中引用/粘贴转写全文**，不管文本长短
  - **不添加模板外内容**（如性能对比表格、分段详情 JSON、加速比等）
  - 批量 → 逐个发送 txt 文件 + 汇总统计

## 与其他 Skill 的协作

| 协作场景 | 进入的 Skill 规程 | 状态 | 时机 | 交接输入 | 交接输出 |
|---------|------------------|------|------|---------|---------|
| 文件格式/时长预检查 | `funasr-task-manager-media-preflight` | ✅ 可用 | Phase 2 | 文件路径 | `ready` / `duration_sec` / `needs_conversion` / `warnings` |
| 任务监控与结果交付 | `funasr-task-manager-result-delivery` | ✅ 可用 | Phase 5 | `task_id(s)` / `task_group_id` / 用户偏好格式 | 转写文本 / 质量报告 / 文件附件 |
| 测试场景需要清库 | `funasr-task-manager-reset-test-db` | ✅ 可用 | Agent 判断需要干净环境时 | 脚本参数 | JSON 状态报告 |
| 验证端到端链路 | `funasr-task-manager-web-e2e` | ✅ 可用 | 发布前验证 | profile 名称 | 测试报告 |
| 用户声明保密/加密 | `funasr-task-manager-secure-ingest` | ⏳ 未创建 | Phase 1 识别到敏感关键词时 | — | **当前 fallback**：见下方说明 |
| 无可用服务器/性能未知 | `funasr-task-manager-server-benchmark` | ✅ 可用 | 用户确认后，或测试/闲时场景 | `server_id`（可选） | benchmark 结果 / 推荐并发数 |

### 尚未创建的 Skill 的 Fallback 行为

**`funasr-task-manager-secure-ingest`（P2，尚未创建）：**

当用户提到保密/加密/敏感关键词，或文件扩展名为 `.enc`/`.aes`/`.gpg` 时，Agent **不可假装具备安全处理能力**。当前应：

1. 告知用户："当前系统尚未支持加密文件处理管道。"
2. 提供替代方案：
   - 在本地完成转写（CLI + 本地 FunASR 服务）
   - 等待加密功能上线后再提交
   - 用户显式确认风险后以明文模式处理
3. 不静默回退到明文流程，不假装已加密

> 注意：用户实时转写链路中，如果服务器缺少 `rtf_baseline`，不应自动启动 benchmark 阻塞任务；应提示"性能基线未校准，将使用默认估算"，除非用户明确要求先校准。此时可引导用户进入 `funasr-task-manager-server-benchmark` 规程。

## 错误处理规范

| 错误场景 | Agent 应做的事 | 不应做的事 |
|---------|--------------|----------|
| 上传接口 400 | 报告"文件格式不支持"，列出支持的格式 | 静默重试或猜测格式 |
| 上传接口 413 | 报告"文件过大"，告知大小限制 | 尝试压缩音频 |
| 服务器全部 OFFLINE | 报告"所有转写服务器不可用" | 继续提交任务 |
| 任务长时间 QUEUED | 报告"等待中，前方有 N 个任务" | 反复取消重建任务 |
| 转写结果为空 | 报告"未检测到语音内容"，建议检查音频 | 假装成功 |
| API 认证失败 401 | 提示检查 API Key 配置 | 自行生成 Key |

## 相关文件

- `references/project-context.md`：项目上下文（symlink → `6-skills/_shared/references/project-context.md`）
- `references/trigger-keywords.json`：触发关键词与权重
- `references/response-templates.md`：用户交互模板
