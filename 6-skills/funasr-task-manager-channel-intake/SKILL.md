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
- 高置信度 → 跳过确认，直接到 Phase 2
- 中/低置信度 → 主动询问用户
- 超时无响应 → 不执行，记录日志

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
- 向用户返回（**严格按 `funasr-task-manager-result-delivery` 的输出模板**）
  - 发送固定格式摘要消息（文件名、时长、格式、转写耗时、文本长度、结果文件名）
  - 以 **txt 文件附件** 形式发回原渠道，文件名与用户发送的原始文件名一致（仅换扩展名为 `.txt`）
  - **不在消息中引用/粘贴转写全文**，不管文本长短
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
