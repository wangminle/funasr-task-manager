# 智能体入口 Skill 体系设计

> **日期**：2026-04-16
> **范围**：funasr-task-manager 项目的 Agent Skill 体系规划——从 channel 收取音频到转写交付的完整自主工作流
> **目标读者**：参与项目的 Agent（子 agent）、开发者、架构评审者
> **前置依赖**：现有后端 API（FastAPI）、前端（Vue 3 + Element Plus）、CLI、调度器、FunASR WebSocket 适配层

---

## 一、背景与动机

### 1.1 当前系统的交互模型

funasr-task-manager 目前有三个入口供人类用户使用：

```
人类用户 ──→ 浏览器（/upload 页面）     ──→ REST API ──→ 调度 ──→ FunASR
人类用户 ──→ CLI（python -m cli transcribe） ──→ REST API ──→ 调度 ──→ FunASR
人类用户 ──→ 第三方调用（curl / httpx）  ──→ REST API ──→ 调度 ──→ FunASR
```

这三条路径都假设 **人类主动发起操作**：手动上传文件、手动提交任务、手动查看结果。

### 1.2 新的交互模型：Agent 作为入口

当 Agent 接入消息平台（飞书 / Telegram / WhatsApp 等）后，交互模型变成：

```
用户在 channel 中 ─┬─ 发送音频/视频文件
                   ├─ 说"帮我转写这段录音"
                   ├─ 说"批量跑一下这些文件"
                   └─ 说"这是保密会议录音，加密处理"
        │
        ▼
   中转平台（OpenClaw / Hermes / 自建 Bot）
        │
        ▼
   Agent（需要一套 Skill 体系来驱动决策和执行）
        │
        ▼
   funasr-task-manager（REST API + 调度 + FunASR）
        │
        ▼
   结果回传 → channel → 用户
```

Agent 需要从 **被动等命令** 变成 **主动感知、判断、编排、执行、交付**。这要求一套结构化的 Skill 来覆盖完整链路。

### 1.3 已有 Skill 覆盖范围

| Skill | 定位 | 覆盖链路 |
|-------|------|---------|
| `funasr-task-manager-reset-test-db` | 测试环境准备 | 测试前置 |
| `funasr-task-manager-web-e2e` | 浏览器 E2E 验收 | 测试验证 |

已有 Skill 全部服务于 **测试辅助** 层，尚无覆盖 **运行时交互** 的 Skill。

---

## 二、Skill 体系总览

### 2.1 四层架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  Layer 4：安全层                                                      │
│  funasr-task-manager-secure-ingest                                          │
│  职责：加密协商、安全接收、明文生命周期管理、结果保护                      │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 3：运行时闭环层                                                  │
│  funasr-task-manager-channel-intake        funasr-task-manager-result-delivery                   │
│  入口编排：意图识别 → 参数协商 → 任务创建                                 │
│  出口交付：进度监控 → 结果下载 → 质量初筛 → 用户反馈                       │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 2：能力基础层                                                   │
│  funasr-task-manager-media-preflight          funasr-task-manager-server-benchmark              │
│  媒体文件预检查与元数据提取         服务器性能基线校准与能力评估             │
├──────────────────────────────────────────────────────────────────────┤
│  Layer 1：测试保障层（已有）                                             │
│  funasr-task-manager-reset-test-db        funasr-task-manager-web-e2e          │
│  测试环境重置                     浏览器端到端验收                        │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 技能矩阵

| # | Skill 名称 | 一句话定位 | 优先级 | 状态 |
|---|-----------|----------|--------|------|
| 1 | `funasr-task-manager-channel-intake` | 音频入口意图编排——从 channel 感知到任务提交 | **P0** | ✅ 已建 |
| 2 | `funasr-task-manager-result-delivery` | 结果交付与质量初筛——从任务完成到用户反馈 | **P0** | ✅ 已建 |
| 3 | `funasr-task-manager-media-preflight` | 媒体文件预检查——格式/时长/转码需求/ffmpeg 可用性 | **P0** | ✅ 已建 |
| 4 | `funasr-task-manager-server-benchmark` | 服务器能力校准——安全发起、解读结果、写回基线 | **P1** | ✅ 已建 |
| 5 | `funasr-task-manager-secure-ingest` | 安全媒体接入——流程约束层，等待后端加密能力 | **P2** | 待建 |
| 预留 | `funasr-task-manager-frontend-audio-intake` | 前端音频入口开发辅助——Vue/API/认证/E2E 约束 | P2 | 暂不创建 |
| 已有 | `funasr-task-manager-reset-test-db` | 测试前数据库与运行时重置 | - | ✅ 已有 |
| 已有 | `funasr-task-manager-web-e2e` | 浏览器端到端测试 | - | ✅ 已有 |

### 2.3 规程切换关系

```
用户消息/文件
      │
      ▼
Agent 加载 funasr-task-manager-channel-intake 规程 ───────────────┐
      │                                               │
      │ ① 检测到敏感关键词？                            │
      │    是 → 切换到 funasr-task-manager-secure-ingest 规程  │
      │    否 → 继续                                   │
      │                                               │
      │ ② 有媒体文件？                                  │
      │    是 → 进入 funasr-task-manager-media-preflight 规程        │
      │    否 → 提示用户发送文件                         │
      │                                               │
      │ ③ 需要检查服务器状态？                           │
      │    是 → GET /api/v1/servers                    │
      │    无可用服务器 → 提示用户                       │
      │                                               │
      │ ④ 提交任务 → 交接给 funasr-task-manager-result-delivery 规程  │
      └──────────────────────────────────────────────┘

funasr-task-manager-server-benchmark ← 独立触发（外部调度/手动/测试前）
      │
      │ 执行前检查 → benchmark → 解读 → 写回 DB
      │
      └→ 结果记录到 4-tests/batch-testing/outputs/benchmark/
```

### 2.4 Skill 边界原则

| Skill | 应负责 | 不应负责 |
|-------|--------|----------|
| `funasr-task-manager-channel-intake` | 识别 channel 意图、主动询问、参数协商、提交任务 | 长期轮询、结果复核、加密协议细节、benchmark 细节 |
| `funasr-task-manager-result-delivery` | 任务监控、结果下载、格式导出、空文本/乱码初筛、用户回传 | 判断是否启动 ASR、重新上传文件、服务器性能校准 |
| `funasr-task-manager-media-preflight` | 文件格式、大小、时长、转码需求、ETA 风险提示 | 创建任务、选择服务器、交付结果 |
| `funasr-task-manager-server-benchmark` | 判断是否可安全测速、发起 benchmark、解析 NDJSON、解释推荐并发变化 | 在用户实时转写链路中无授权阻塞任务、替代调度器决策 |
| `funasr-task-manager-secure-ingest` | 敏感意图识别、风险拦截、加密流程约束、未来加密管道编排 | 在后端无能力时假装加密处理、承诺物理不可恢复删除 |
| `funasr-task-manager-frontend-audio-intake` | Agent 开发前端入口时提供 Vue/API/认证/E2E 约束 | 运行时接收用户文件、执行转写任务 |

---

## 三、Skill 1：`funasr-task-manager-channel-intake`（音频入口与意图编排）

### 3.1 定位

这是整个 Skill 体系中最核心的入口。它让 Agent 从"被动等命令"变成"看到文件后主动引导用户完成转写"。

它的职责不是"检测何时触发"，而是 **识别用户意图并完成最小必要的入口编排**：判断是否启动 ASR、完成参数协商、执行预检查、提交任务，并把后续监控与结果交付交给 `funasr-task-manager-result-delivery`。

> MVP 阶段可把进度监控和结果交付临时写在此 Skill 中，但文档和实现都应保留 `funasr-task-manager-result-delivery` 的拆分边界，避免入口 Skill 演化成巨型 Skill。

### 3.2 触发条件

Agent 应在以下任一条件满足时激活此 Skill：

**文件触发：**

- channel 中出现音视频文件（扩展名匹配 `.wav` / `.mp3` / `.mp4` / `.flac` / `.ogg` / `.webm` / `.m4a` / `.aac` / `.wma` / `.mkv` / `.avi` / `.mov` / `.pcm`）

**关键词触发：**

- 用户消息包含：`转写` / `识别` / `字幕` / `转文字` / `语音转文本` / `ASR` / `FunASR` / `批量任务` / `transcribe` / `subtitle`
- 用户消息包含 profile 关键词：`smoke` / `remote-standard` / `standard` / `full`

**组合触发：**

- 文件 + 任何文字说明（即便不含关键词，有文件就应主动询问）
- 引用/转发了包含音视频的消息

**不触发：**

- 纯文本闲聊
- 图片文件（.jpg / .png / .gif 等）
- 文档文件（.pdf / .docx / .xlsx 等），除非用户明确提及转写

### 3.3 三层感知模型

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
  └─ 用户提到敏感/保密？ → 切换到 funasr-task-manager-secure-ingest

Layer C：决策（Decision）
  ├─ 单文件还是批量？
  ├─ 需要什么输出格式？
  ├─ 需要先检查服务器状态吗？
  └─ 可以直接提交还是需要更多参数？
```

### 3.4 完整交互流程

```
Phase 1：意图确认
  ├─ 识别文件和任务意图
  ├─ 高置信度 → 跳过确认，直接到 Phase 2
  ├─ 中/低置信度 → 主动询问用户
  └─ 超时无响应 → 不执行，记录日志

Phase 2：预检查
  ├─ 按 funasr-task-manager-media-preflight 规程执行预检查（文件格式、大小、时长、转码需求）
  ├─ 检查 ffmpeg/ffprobe 可用性
  ├─ 检查后端服务健康：GET /health → status: "ok"
  ├─ 检查可用服务器：GET /api/v1/stats → server_online > 0
  │   ⚠ 注意：GET /api/v1/servers 依赖 AdminUser 认证，普通 API Key 会返回 403。
  │   运行时可用性判断应使用无需 admin 权限的 /api/v1/stats（返回 server_online/server_total）。
  │   如需获取完整服务器列表（如排查调度问题），Agent 必须持有 admin token。
  └─ 预检查失败 → 明确告知用户哪个环节有问题

Phase 3：参数协商
  ├─ 询问输出格式（有默认值，非强制交互）
  │   └─ 默认：txt + json，用户可选 srt
  ├─ 判断单文件 / 批量任务
  │   ├─ 1 个文件 → 单文件流程
  │   └─ 多个文件 → 批量流程，自动生成 task_group_id
  ├─ 特殊参数（如有）：语言、热词列表、回调 URL
  └─ 如果用户说"默认就行" → 全部使用默认值

Phase 4：任务提交
  ├─ 上传文件：POST /api/v1/files/upload
  │   └─ 记录 file_id
  ├─ 创建任务：POST /api/v1/tasks
  │   └─ body:
  │      {
  │        "items": [
  │          {"file_id": "...", "language": "zh", "options": {...}}
  │        ],
  │        "callback": {"url": "...", "secret": "..."}   # 可选
  │      }
  │   └─ 记录 task_id(s) 和 task_group_id
  ├─ 向用户确认："已提交 N 个文件，任务编号 xxx"
  └─ 提交失败 → 报告错误原因

Phase 5：移交结果交付
  ├─ 将上下文交接给 funasr-task-manager-result-delivery 的执行流程
  ├─ 交接字段：task_id(s)、task_group_id、用户偏好格式、channel 回传方式
  └─ 若 funasr-task-manager-result-delivery 尚未创建，可临时在本 Skill 内执行下方 Phase A-B
```

### 3.5 临时内置的结果交付流程（未来拆入 `funasr-task-manager-result-delivery`）

```
Phase A：进度监控
  ├─ 轮询任务状态（5-10 秒间隔）
  │   └─ GET /api/v1/tasks/{task_id}
  ├─ 或通过 SSE 监听：GET /api/v1/tasks/{task_id}/progress
  ├─ 关键状态变化时通知用户：
  │   ├─ PREPROCESSING → "文件预处理中..."
  │   ├─ QUEUED → "等待调度..."
  │   ├─ TRANSCRIBING → "正在转写..."
  │   ├─ SUCCEEDED → "转写完成！"
  │   └─ FAILED → "转写失败：{原因}"
  ├─ 批量任务：汇报完成进度 "3/5 已完成"
  └─ 超时策略：
      ├─ 单文件：5 分钟超时
      ├─ 批量 ≤ 5 个：20 分钟超时
      └─ 批量 > 5 个：按文件数 × 5 分钟

Phase B：结果交付
  ├─ 拉取结果
  │   ├─ 单任务：GET /api/v1/tasks/{task_id}/result?format=txt
  │   └─ 批量：GET /api/v1/task-groups/{group_id}/results?format=zip
  ├─ 结果质量初筛
  │   ├─ 空文本 → 标记异常，建议重试或检查音频质量
  │   ├─ 明显乱码 → 标记异常
  │   └─ 正常 → 继续交付
  ├─ 向用户返回
  │   ├─ 短文本（< 500 字）→ 直接在 channel 中发送
  │   ├─ 长文本 → 发送文件 + 摘要
  │   └─ 批量 → 发送 zip + 汇总统计
  └─ 汇总信息模板：
      "✅ 转写完成
       文件: {filename}
       时长: {duration}s
       RTF: {rtf}
       文本长度: {text_length} 字
       耗时: {elapsed}s"
```

### 3.6 与其他 Skill 的协作

| 协作场景 | 进入的 Skill 规程 | 时机 | 交接输入 | 交接输出 |
|---------|------------------|------|---------|---------|
| 文件格式/时长预检查 | `funasr-task-manager-media-preflight` | Phase 2 | 文件路径 | `ready` / `duration_sec` / `needs_conversion` / `warnings` |
| 用户声明保密/加密 | `funasr-task-manager-secure-ingest` | Phase 1 识别到敏感关键词时 | 密文文件路径 | 解密后临时文件路径，或拦截提示 |
| 任务监控与结果交付 | `funasr-task-manager-result-delivery` | Phase 5 | `task_id(s)` / `task_group_id` / 用户偏好格式 | 转写文本 / 质量报告 / 文件附件 |
| 无可用服务器/性能未知 | `funasr-task-manager-server-benchmark` | 用户确认后，或测试/闲时场景 | `server_id`（可选） | benchmark 结果 / 推荐并发数 |
| 测试场景需要清库 | `funasr-task-manager-reset-test-db` | Agent 判断需要干净环境时 | 脚本参数 | JSON 状态报告 |
| 验证端到端链路 | `funasr-task-manager-web-e2e` | 发布前验证 | profile 名称 | 测试报告 |

> 注意：用户实时转写链路中，如果服务器缺少 `rtf_baseline`，不应自动启动 benchmark 阻塞任务；应提示"性能基线未校准，将使用默认估算"，除非用户明确要求先校准。

### 3.7 错误处理规范

| 错误场景 | Agent 应做的事 | 不应做的事 |
|---------|--------------|----------|
| 上传接口 400 | 报告"文件格式不支持"，列出支持的格式 | 静默重试或猜测格式 |
| 上传接口 413 | 报告"文件过大"，告知大小限制 | 尝试压缩音频 |
| 服务器全部 OFFLINE | 报告"所有转写服务器不可用" | 继续提交任务 |
| 任务长时间 QUEUED | 报告"等待中，前方有 N 个任务" | 反复取消重建任务 |
| 转写结果为空 | 报告"未检测到语音内容"，建议检查音频 | 假装成功 |
| API 认证失败 401 | 提示检查 API Key 配置 | 自行生成 Key |

### 3.8 SKILL.md 元信息建议

```yaml
---
name: funasr-task-manager-channel-intake
description: >
  Recognize transcription intent and orchestrate task submission when audio/video
  files or ASR keywords appear in a channel. Use when: audio/video files arrive,
  user mentions transcription/ASR/subtitle, user asks to batch-process media,
  or agent needs to proactively guide a user into the transcription pipeline.
---
```

---

## 四、Skill 2：`funasr-task-manager-result-delivery`（结果交付与质量初筛）

### 4.1 定位

`funasr-task-manager-result-delivery` 是运行时闭环的出口 Skill。它负责在任务创建后监控状态、拉取结果、做基础质量检查，并把结果以合适形式返回到 channel。

它应能被两类场景触发：

- 由 `funasr-task-manager-channel-intake` 在任务提交后自动调用。
- 用户针对已有任务或批次请求"重新发结果"、"导出 srt"、"检查质量"。

### 4.2 触发条件

**自动触发：**

- `funasr-task-manager-channel-intake` 成功创建任务后，传入 `task_id(s)` 或 `task_group_id`。
- 批量任务提交成功后，需要持续回报完成进度。

**用户显式触发：**

- 用户说"把这个任务结果发我"/"重新导出字幕"/"下载 json"/"检查这批结果质量"
- 用户提供 `task_id` 或 `task_group_id`

**关键词：**

- `结果` / `导出` / `下载` / `字幕` / `srt` / `json` / `txt` / `质量` / `乱码` / `空文本`

### 4.3 执行流程

```
Phase 1：接收任务上下文
  ├─ 输入：task_id(s) 或 task_group_id
  ├─ 输入：期望格式 txt/json/srt/zip
  ├─ 输入：channel 回传能力（可发文本/可发文件/大小限制）
  └─ 缺少任务标识 → 向用户询问

Phase 2：监控任务状态
  ├─ 单任务：GET /api/v1/tasks/{task_id}
  ├─ 批量：GET /api/v1/task-groups/{group_id}
  ├─ 可选：GET /api/v1/tasks/{task_id}/progress
  ├─ 状态变化时回报关键节点
  └─ 超时后给出当前状态和下一步建议，不盲目取消任务

Phase 3：拉取结果
  ├─ 单任务：GET /api/v1/tasks/{task_id}/result?format=txt/json/srt
  ├─ 批量：GET /api/v1/task-groups/{group_id}/results?format=txt/json/srt/zip
  └─ 无成功任务 → 返回失败摘要，不假装成功

Phase 4：质量初筛
  ├─ 空文本 → 标记异常，建议检查音频是否静音或语言/模型是否匹配
  ├─ 明显乱码 → 标记异常，建议检查编码、音频质量或输入格式
  ├─ 文本过短 → 提醒可能是静音、噪声或截断
  ├─ 批量任务 → 汇总成功/失败/空文本数量
  └─ 正常 → 进入交付

Phase 5：结果交付
  ├─ 短文本（默认 < 500 字）→ 直接发送到 channel
  ├─ 长文本 → 发送文件 + 摘要
  ├─ SRT/JSON → 优先作为附件返回
  ├─ 批量 → 发送 zip 或汇总文件
  └─ 安全模式 → 将结果交给 funasr-task-manager-secure-ingest 做加密返回
```

### 4.4 输出摘要模板

```
✅ 转写完成

文件: {filename}
状态: SUCCEEDED
服务器: {assigned_server_id}
音频时长: {duration_sec}s
处理耗时: {elapsed_sec}s
RTF: {rtf}
文本长度: {text_length} 字
结果格式: txt/json/srt

质量提示:
- 文本非空
- 未发现明显乱码
```

### 4.5 失败处理规范

| 场景 | Agent 应做的事 | 不应做的事 |
|------|--------------|----------|
| 任务仍在运行 | 告知当前状态和预计等待方式 | 重复创建任务 |
| 任务失败 | 返回 error_message，并建议是否重试 | 隐藏失败原因 |
| 没有成功任务 | 返回批次失败摘要 | 返回空 zip 当作成功 |
| 文本为空 | 标记质量异常 | 直接说"转写成功" |
| 用户要求安全返回 | 切换到 `funasr-task-manager-secure-ingest` | 直接明文贴回 channel |

### 4.6 SKILL.md 元信息建议

```yaml
---
name: funasr-task-manager-result-delivery
description: >
  Monitor transcription tasks and deliver results with quality checks.
  Use when: task_id or task_group_id needs monitoring or result return,
  user asks to export txt/json/srt/zip, user requests quality check on
  transcription output, or results need re-delivery for an existing task.
---
```

---

## 五、Skill 3：`funasr-task-manager-server-benchmark`（服务器能力校准）

### 5.1 定位

Benchmark 是调度准确性的基础设施。它写回三个关键字段：

- **`rtf_baseline`**（= benchmark 的 `single_rtf`）：直接影响调度器 ETA 预估（`get_effective_rtf()` 使用生产 P90 或 `rtf_baseline` 回退值）和配额分配（`get_throughput_speed()` = `max_concurrency / base_rtf`）。
- **`max_concurrency`**（= benchmark 的 `recommended_concurrency`）：决定服务器可用 slot 数量，直接影响配额分配和并发调度。
- **`throughput_rtf`**：并发吞吐量基准，当前主要作为 benchmark 结果记录和容量对比指标（`capacity_comparison`），**不直接参与调度计算**（调度器注释明确说明 `throughput_rtf` 因短样本偏差不适合替代 `rtf_baseline`）。除非后续代码改为直接使用。

这个 Skill 不只是"发起 benchmark"——它是 **安全发起 + 解读结果 + 写回校准** 的完整 workflow。

### 5.2 触发条件

**外部调度触发（需 orchestrator 唤醒 Agent）：**

- 外部 orchestrator（CI / cron / Bot）在低峰时段唤醒 Agent 执行校准
- 新服务器注册后，orchestrator 或用户指示校准
- 距上次 benchmark 超过设定周期（由外部调度判断，非 Skill 自行检测）
- 调度器 ETA 预估偏差持续 > 30%，运维或监控系统触发校准

**被动触发（用户/开发者显式请求）：**

- 用户说"跑一下 benchmark"/"测试一下服务器性能"
- 测试前需要确保调度基线可信
- 注册服务器时选择了 `--benchmark` 参数

**关键词：**

- `benchmark` / `测速` / `性能测试` / `基准测试` / `校准` / `calibrate`
- `服务器性能` / `RTF` / `吞吐量` / `并发测试`

> 约束：如果用户正在提交实时转写任务，而服务器只是缺少 `rtf_baseline`，此 Skill 不应自动阻塞用户任务去跑 benchmark。应使用默认基线估算并提示"性能基线未校准"，只有用户确认或进入测试/闲时校准场景时才执行。

### 5.3 安全约束——Benchmark 不能随便跑

这是此 Skill 最关键的设计约束。Benchmark 本身会占用服务器资源，如果在高负载时发起，会干扰正在执行的真实转写任务。

**Benchmark 前置检查清单：**

```
CHECK 1：任务队列状态
  ├─ GET /api/v1/stats → 检查 slots_used / queue_depth
  ├─ 如果 slots_used > 0 或 queue_depth > 0
  │   └─ 警告 "当前有 {slots_used} 个占用 slot、{queue_depth} 个排队任务，benchmark 可能影响转写性能"
  │   └─ 用户确认后才继续；外部调度触发的校准场景直接放弃
  └─ 队列为空 → 安全，继续

CHECK 2：目标服务器状态
  ├─ GET /api/v1/servers → 检查目标服务器 status（⚠ 需 admin token）
  │   或用 GET /api/v1/stats 的 server_online 做粗粒度判断（无需 admin）
  ├─ OFFLINE → 先 probe，不直接 benchmark
  ├─ DEGRADED → 警告 "服务器处于降级状态"
  └─ ONLINE → 继续
  注：benchmark 端点本身也需要 AdminUser 认证，因此执行 benchmark 的 Agent 必须持有 admin token。

CHECK 3：距上次 benchmark 的间隔
  ├─ 如果 < 10 分钟 → 跳过 "刚跑过，无需重复"
  └─ 否则 → 继续
```

### 5.4 执行流程

```
Phase 1：前置检查（见 5.3）

Phase 2：选择 Benchmark 范围
  ├─ 单服务器：POST /api/v1/servers/{server_id}/benchmark
  ├─ 全部服务器（并发压测）：POST /api/v1/servers/benchmark
  │   ⚠ 注意：此端点会为所有 ONLINE 节点创建并发 benchmark 任务，
  │   所有节点同时承受压力。仅适用于"用户明确请求全量压测"的场景。
  └─ 安全校准（逐个顺序）：循环调用 POST /api/v1/servers/{id}/benchmark
      适用于闲时校准场景，逐个执行避免同时压满多台服务器。

Phase 3：实时进度解读（NDJSON 流式）
  ├─ 解析每一行 NDJSON 事件：
  │   进度事件（单节点和全量共享）：
  │   ├─ benchmark_start → "开始 benchmark，共 2 阶段"
  │   ├─ phase_start → "Phase 1: 单线程测速..."
  │   ├─ phase_progress → "第 1/2 次采样: RTF = 0.1256"
  │   ├─ phase_complete → "Phase 1 完成: 单线程 RTF = 0.1234"
  │   ├─ gradient_start → "并发梯度 N=4（3/4）"
  │   ├─ gradient_complete → "N=4: throughput_rtf = 0.0358"
  │   ├─ gradient_error → "N=8: 退化检测触发"
  │   └─ benchmark_complete → 服务层进度事件，表示一次 benchmark 流程完成（非最终结果）
  │
  │   终结事件（按接口区分）：
  │   ├─ 单节点接口（POST /servers/{id}/benchmark）：
  │   │   └─ benchmark_result（成功，含完整 ServerBenchmarkItem）或 benchmark_error（失败）
  │   └─ 全量接口（POST /servers/benchmark）：
  │       ├─ all_benchmark_start → 列出即将测试的 server_ids
  │       ├─ server_benchmark_done → 单节点完成（含 completed/total 计数）
  │       ├─ server_error → 单节点失败
  │       └─ all_complete → 最终聚合结果（含 results[] 和 capacity_comparison[]）
  └─ 定期向用户/调用者汇报进度

Phase 4：结果解读与校准
  ├─ 解读 single_rtf（单线程处理速度）
  ├─ 解读 throughput_rtf（吞吐量，越低越快）
  ├─ 解读 recommended_concurrency（推荐并发数）
  ├─ 如果检测到退化（某梯度级别性能下降 > 10%）→ 解释原因
  │   ├─ 可能原因：服务器资源不足、网络延迟（LAN vs WAN）、模型加载慢
  │   └─ 建议：降低 max_concurrency / 检查网络 / 检查 GPU 显存
  └─ 自动写回 DB（benchmark 接口已内置）

Phase 5：记录与归档
  ├─ 生成 benchmark 报告
  ├─ 保存到 4-tests/batch-testing/outputs/benchmark/
  │   └─ 文件名格式：benchmark-{server_id}-{YYYYMMDD-HHmmss}.json
  └─ 与历史 benchmark 对比（如有），检测长期性能趋势
```

### 5.5 结果解读模板

```
✅ Benchmark 完成: asr-10095

┌───────────────────────────────────────┐
│ 单线程 RTF     │ 0.1234              │
│ 吞吐量 RTF     │ 0.0358              │
│ 推荐并发数      │ 4                   │
│ 测试样本       │ tv-report-1.wav      │
│ 耗时           │ 3m 42s              │
├───────────────────────────────────────┤
│ 并发梯度详情                           │
│  N=1: tp_rtf=0.1180, wall=0.72s      │
│  N=2: tp_rtf=0.0634, wall=0.77s      │
│  N=4: tp_rtf=0.0358, wall=0.87s ← 推荐│
│  N=8: ⚠ 退化 (improvement < 10%)     │
├───────────────────────────────────────┤
│ 调度影响                               │
│  rtf_baseline ← 0.1234（影响 ETA）    │
│  max_concurrency ← 4（影响 slot 数）  │
│  throughput_rtf ← 0.0358（容量对比用） │
│  get_effective_rtf() 使用 rtf_baseline │
│   或生产 P90 回退                      │
│  get_throughput_speed() = 4 / 0.1234  │
│   = 32.4（配额速度）                  │
└───────────────────────────────────────┘
```

### 5.6 外部调度触发的闲时校准场景

> **前提**：Skill 本身不具备定时调度能力，不会自主"醒来"。闲时校准必须由外部 orchestrator（CI 定时任务、Bot 编排器、系统 cron、运维手动触发）唤醒 Agent 并提供校准指令，Agent 再加载本 Skill 执行。

```
场景：外部 orchestrator 在夜间触发 Agent 执行服务器校准

前提：
  - 外部 orchestrator（CI / cron / Bot）在低峰时段唤醒 Agent
  - Agent 收到"校准服务器 benchmark"指令后加载本 Skill

执行：
  1. CHECK 任务队列 → 确认为空（如不为空，放弃并报告）
  2. GET /api/v1/servers → 获取所有 ONLINE 服务器列表（需 admin token）
  3. 循环调用 POST /api/v1/servers/{id}/benchmark 逐个校准
     ⚠ 不可使用 POST /api/v1/servers/benchmark（全量并发端点），
     否则所有服务器同时承受压力，违背"避免压满多台服务器"的安全约束。
  4. 每个节点完成后记录结果，与上次对比
  5. 如果 RTF 偏差 > 20% → 标记为 DEGRADED，通知运维
  6. 否则 → 静默更新基线
```

### 5.7 SKILL.md 元信息建议

```yaml
---
name: funasr-task-manager-server-benchmark
description: >
  Safely benchmark FunASR servers and calibrate scheduling baselines.
  Use when: a server has no rtf_baseline, user requests benchmark or
  performance test, external orchestrator triggers idle-time calibration,
  or ETA predictions consistently deviate from actual processing time.
---
```

---

## 六、Skill 4：`funasr-task-manager-media-preflight`（媒体文件预检查）

### 6.1 定位

前端"收取音频入口"最容易出问题的不是提交任务，而是 **文件进入系统前的判断**。这个 Skill 单独负责文件级别的预检查，可以被 `funasr-task-manager-channel-intake` 调用，也可以独立用于排查文件问题。

### 6.2 触发条件

- `funasr-task-manager-channel-intake` 在 Phase 2 自动调用
- 用户问"为什么这个文件上传后时长不准"/"这个文件能转写吗"
- Agent 需要在提交任务前评估文件可行性
- 关键词：`预检查` / `文件检查` / `格式` / `时长` / `转码` / `ffprobe`

### 6.3 检查清单

```
CHECK 1：文件存在性与完整性
  ├─ 文件是否存在？
  ├─ 文件大小是否 > 0？
  ├─ 文件大小是否超过上限？（默认由 settings.max_upload_size_mb 控制）
  └─ 失败 → 报告具体原因

CHECK 2：格式识别
  ├─ 扩展名是否在允许列表中？
  │   └─ 允许列表（与后端 file_manager.py 一致）：
  │      .wav .mp3 .mp4 .flac .ogg .webm .m4a .aac .wma .mkv .avi .mov .pcm
  ├─ 扩展名不匹配 → 报告 "不支持的格式: .xxx"，列出支持的格式
  └─ 可选：通过 file magic bytes 二次验证（防止扩展名伪造）

CHECK 3：元数据提取
  ├─ 检查 ffprobe 是否可用
  │   └─ 不可用 → 标记 precise_metadata=false，并说明 ETA 只能估算
  ├─ 调用 ffprobe 获取：
  │   ├─ duration（时长，秒）
  │   ├─ codec_name（编码格式）
  │   ├─ sample_rate（采样率）
  │   ├─ channels（声道数）
  │   ├─ bit_rate（比特率）
  │   └─ format_name（容器格式）
  └─ ffprobe 失败 → 不直接判定不可处理；按文件大小估算 duration，提醒 "精确元数据不可用，仍可提交但 ETA 不准"

CHECK 4：转码需求评估
  ├─ 判断是否需要转码（与 audio_preprocessor.py 的 needs_conversion() 逻辑一致）
  │   ├─ 当前实现：仅按扩展名判断——.wav 和 .pcm 不转码，其他格式一律转码
  │   ├─ 非 WAV/PCM → 需要转码（ffmpeg 会统一转为 16kHz 单声道 s16 WAV）
  │   └─ .wav / .pcm → 直接使用（即使采样率≠16000 或多声道，当前也不会触发转码）
  ├─ ⚠ 已知局限：WAV 文件的采样率/声道数重采样是目标能力，当前未实现。
  │   preflight 可以在 warnings 中提示"WAV 文件采样率非 16kHz / 多声道，FunASR 可能处理异常"，
  │   但不应断言后端会自动重采样。
  ├─ 需要转码 → 报告 "文件需要预处理（转码到 16kHz 单声道 WAV）"
  └─ 估算转码耗时（粗略：文件大小 / 10MB ≈ 秒数）

CHECK 5：任务耗时预估
  ├─ 如果服务器有 rtf_baseline → estimated_time = duration × rtf_baseline
  ├─ 如果无基线 → estimated_time = duration × DEFAULT_RTF (0.3)
  ├─ 加上转码耗时（如需要）
  └─ 报告 "预计处理时间: {estimated_time}s"

CHECK 6：风险提醒
  ├─ 时长 > 1 小时 → 提醒 "超长音频，预计耗时较久"
  ├─ 文件 > 500MB → 提醒 "超大文件，上传和转码耗时较长"
  ├─ 编码为无损格式（FLAC/WAV）且文件很大 → 正常
  ├─ 编码为压缩格式但文件异常大 → 提醒 "文件可能包含视频轨道"
  └─ 视频文件 → 提醒 "视频文件将提取音频轨道后转写"
```

### 6.4 输出格式

```json
{
  "filename": "会议录音-20260415.mp4",
  "file_size_mb": 156.3,
  "format": "mp4",
  "duration_sec": 3720.5,
  "precise_metadata": true,
  "duration_human": "1h 2m 0s",
  "codec": "aac",
  "sample_rate": 44100,
  "channels": 2,
  "needs_conversion": true,
  "conversion_reason": "非 WAV 格式，需转码到 16kHz 单声道 WAV",
  "estimated_conversion_sec": 15,
  "estimated_processing_sec": 1116,
  "estimated_processing_human": "约 18 分钟",
  "warnings": [
    "超长音频（> 1 小时），耗时较久",
    "视频文件，将提取音频轨道"
  ],
  "ready": true
}
```

### 6.5 用户友好的汇报模板

```
📋 文件预检查: 会议录音-20260415.mp4

  格式: MP4 (AAC 音频)
  时长: 1h 2m 0s
  大小: 156.3 MB
  声道: 2（立体声）
  采样率: 44100 Hz

  ⚙ 需要预处理: 是（转码到 16kHz 单声道 WAV）
  ⏱ 预计转码: ~15s
  ⏱ 预计转写: ~18 分钟

  ⚠ 注意: 超长音频，耗时较久
  ⚠ 注意: 视频文件，将提取音频轨道

  ✅ 可以提交转写
```

### 6.6 SKILL.md 元信息建议

```yaml
---
name: funasr-task-manager-media-preflight
description: >
  Pre-check audio/video files before transcription submission.
  Use when: validating whether a file can be transcribed, diagnosing
  file metadata or duration issues, estimating processing time before
  committing, or checking ffmpeg/ffprobe availability.
---
```

---

## 七、Skill 5：`funasr-task-manager-secure-ingest`（安全媒体接入 — 当前为风险拦截 Skill）

> **⚠ 当前状态**：funasr-task-manager 后端 **尚未实现** 加密文件处理管道。本 Skill 在当前阶段的实际作用是 **风险拦截与流程约束**——识别敏感场景、阻止不安全操作、告知用户系统能力边界。它 **不是** 一个"启用即安全"的加密处理 Skill。Agent 不应因为触发了此 Skill 就声称系统已经具备端到端加密能力。

### 7.1 定位

当音频文件通过飞书 / Telegram / WhatsApp 等消息平台的 channel 传输时，文件对中转平台是透明的——即便传输层有 TLS，每个中间节点（消息平台、中转平台如 OpenClaw/Hermes）都能看到明文音频内容。

对于涉及法律、医疗、金融、商业机密等场景的音频，仅靠传输加密不够。这个 Skill 的最终目标是管理 **端到端内容保护**——让音频在离开用户设备后，只有 funasr-task-manager 能解密处理。

**当前阶段定位**：纯流程约束层——告诉 Agent 什么时候必须拦截、要告知用户什么风险、哪些操作不可执行。待后端加密管道实现后（见 7.9），再升级为完整的安全处理 Skill。

### 7.2 威胁模型

```
用户设备 ──TLS──→ 消息平台（明文存储/缓存）
                      │
                      ├──webhook──→ 中转平台（明文中转）
                      │                 │
                      │                 └──HTTP──→ funasr-task-manager
                      │                                │
                      │                                ├─ uploads/（明文落盘）
                      │                                ├─ ffmpeg 转码（明文）
                      │                                └─ WebSocket → FunASR（明文）
```

**暴露点：**

| 节点 | 风险 | 加密后是否消除 |
|------|------|--------------|
| 消息平台内部存储 | 平台员工可访问、平台被入侵 | ✅ 文件为密文，平台无法解读 |
| 中转平台内存/磁盘 | 中转平台被入侵 | ✅ 文件为密文，中转平台无法解读 |
| funasr-task-manager uploads/ | 服务器被入侵、日志泄露 | ⚠ 解密后存在短暂明文窗口 |
| FunASR 服务 | FunASR 只接受明文 PCM/WAV | ❌ 无法消除，靠内网隔离 |

**架构约束**：FunASR 引擎不支持加密音频，解密必须在 funasr-task-manager 内部完成。funasr-task-manager 是信任边界的终点。

### 7.3 两层设计

**第一层：Skill 层的流程约束（当前可设计）**

告诉 Agent 什么时候必须进入安全接入流程、要问用户什么、哪些事情不能做。

**第二层：系统能力层的代码实现（当前未实现，需未来开发）**

funasr-task-manager 后端需要支持：加密文件上传 → 密文存储 → 任务执行前临时解密 → 处理 → best-effort 清理明文 → 结果可选加密。

### 7.4 触发条件

**敏感关键词检测：**

- 用户消息包含：`保密` / `加密` / `机密` / `隐私` / `敏感` / `不要落盘` / `私密会议` / `法律录音` / `医疗` / `合同` / `病历` / `内部会议` / `encrypted` / `confidential` / `private`

**显式声明：**

- 用户说"这是加密文件"/"用 AES 加密过的"
- 文件扩展名为 `.enc` / `.aes` / `.gpg` / 自定义加密格式

**协议协商：**

- 用户与 Agent 此前已建立加密会话（session_key 存在）

### 7.5 安全接入流程

```
Phase 1：场景识别
  ├─ 检测到敏感关键词 → 进入安全模式
  ├─ 检测到加密文件格式 → 进入安全模式
  └─ 进入安全模式后，明确告知用户：
     "已进入安全媒体接入模式。当前会先检查后端是否支持加密处理；如不支持，不会静默回退到明文。"

Phase 2：密钥协商
  ├─ 方式 A：预共享密钥（PSK）
  │   └─ 用户提前通过安全渠道（非 channel）提供密码/密钥
  ├─ 方式 B：一次性密钥
  │   └─ Agent 生成随机密钥，通过安全渠道发给用户
  ├─ 方式 C：公钥交换
  │   └─ Agent 提供公钥，用户用公钥加密文件后发送
  └─ 密钥存储：仅在内存中，不持久化到数据库或日志

Phase 3：加密文件接收
  ├─ 接收密文文件
  ├─ 验证完整性（如有 HMAC/签名）
  ├─ 密文临时存储到受保护目录（不放 uploads/）
  └─ 不在日志中记录文件名的敏感部分

Phase 4：解密与处理
  ├─ 在受控临时目录中解密
  ├─ 解密后文件走正常 preflight → upload → task 流程
  ├─ 但源文件路径指向临时解密位置
  └─ 处理过程中的日志：
     ├─ 不记录完整文件路径
     ├─ 不记录转写全文
     └─ 仅记录任务 ID 和状态

Phase 5：安全清理
  ├─ 任务完成后（成功或失败）
  ├─ 对临时明文执行 best-effort 清理（关闭句柄、删除文件、清理中间产物）
  ├─ 优先使用加密临时目录和最短保留时间降低恢复风险
  ├─ 清理密文临时文件
  ├─ 清理 ffmpeg 中间文件
  └─ 记录清理结果，但不承诺 SSD/APFS/容器卷/对象存储上的物理不可恢复

Phase 6：结果保护（可选）
  ├─ 转写结果是否也需要加密？
  │   ├─ 用户要求加密结果 → 用 session_key 加密后返回
  │   └─ 用户未要求 → 明文返回（但提示"结果以明文发送"）
  └─ 结果文件在服务端的保留策略：
     ├─ 默认：24 小时后自动删除
     └─ 安全模式：任务完成即删除，不保留
```

### 7.6 不可妥协的安全约束

| 约束 | 原因 |
|------|------|
| 解密失败时不回退到明文流程 | 防止用户误以为文件已加密处理，实则明文传输 |
| 密钥不持久化到 DB/日志/文件 | 防止密钥泄露 |
| 不在 channel 中传输密钥 | channel 本身不可信 |
| 临时明文只能短暂存在，并执行 best-effort 清理 | SSD、APFS、容器卷、对象存储不保证覆写删除可靠 |
| 安全模式下日志不含文件名/转写文本 | 防止日志泄露敏感内容 |
| Agent 不代用户决定是否加密 | 只有用户能声明敏感性 |

### 7.7 当前阶段的降级策略

由于 funasr-task-manager 后端尚未实现加密文件处理管道，Skill 应明确告知 Agent：

```
IF 后端无加密支持 AND 用户要求加密处理:
  → 告知用户：
    "当前系统尚未支持加密文件处理管道。
     建议方案：
     1. 在本地完成转写（使用 CLI + 本地 FunASR 服务）
     2. 等待加密功能上线后再提交敏感文件
     3. 如果风险可接受，您可以选择以明文模式处理（需您确认）"
  → 不假装已加密，不静默回退到明文
```

### 7.8 安全模式非目标

安全模式需要明确边界，避免过度承诺：

- 不解决消息平台在用户发送前后已保存、缓存或备份的历史明文文件。
- 不解决 FunASR 服务侧明文处理问题；FunASR 引擎当前只能消费明文 PCM/WAV。
- 不承诺 SSD、APFS、容器卷、对象存储上的删除后物理不可恢复。
- 不把普通 channel 当成安全密钥交换通道；密钥必须走独立安全渠道。
- 不替代合规审计、权限治理、数据保留策略和基础设施隔离。

### 7.9 后端加密能力实现路线（规划）

当决定实现后端加密支持时，需要新增：

| 组件 | 文件 | 职责 |
|------|------|------|
| 加密服务 | `app/services/crypto.py` | 密钥管理、加密/解密、best-effort 清理 |
| 安全存储 | `app/storage/secure_file_manager.py` | 受保护临时目录管理 |
| API 扩展 | `app/api/files.py` | 加密上传端点 `POST /api/v1/files/upload-encrypted` |
| 配置 | `app/config.py` | 加密算法、临时目录路径、清理策略 |
| 清理任务 | `app/services/secure_cleanup.py` | 定时扫描并清理遗留明文 |

### 7.10 SKILL.md 元信息建议

```yaml
---
name: funasr-task-manager-secure-ingest
description: >
  Intercept and manage sensitive audio/video when users declare confidentiality.
  Use when: user mentions confidential/encrypted/private media, files arrive
  with encryption markers (.enc/.aes/.gpg), or regulatory compliance requires
  content protection. Currently acts as a risk-interception skill only;
  backend encryption pipeline is not yet implemented.
---
```

---

## 八、已有 Skill 在新体系中的复用

### 8.1 `funasr-task-manager-reset-test-db`

**复用场景：**

- `funasr-task-manager-channel-intake` 在测试模式下，提交任务前可能需要清库
- `funasr-task-manager-server-benchmark` 在测试前确保数据库基线干净
- 子 Agent 跑批量回归前的标准前置步骤

**不需要改动**，当前 Skill 的接口（脚本 + JSON 输出）已经足够其他 Skill 调用。

### 8.2 `funasr-task-manager-web-e2e`

**复用场景：**

- 前端入口改动后的验收测试
- 新 Skill 开发完成后，验证端到端链路未被破坏
- 发布前的质量门禁

**不需要改动**，但未来 `funasr-task-manager-channel-intake` 如果引入了新的前端交互模式（如 channel 消息输入），可能需要扩展 E2E 覆盖范围。

---

## 九、技能间交互协议

### 9.1 协作约定

> **关键概念**：Skill 是"按触发条件加载的一组操作规程"，不是函数、服务或可 RPC 调用的运行时模块。Agent 在执行 Skill A 的过程中如果需要 Skill B 的能力，实际发生的是 **Agent 切换到 Skill B 的规程并按其流程操作**，而不是 Skill A "调用" Skill B。

Skill 之间的协作遵循以下原则：

1. **松耦合**：每个 Skill 都可以独立使用，不强制依赖其他 Skill
2. **规程切换而非函数调用**：当 Agent 需要从一个 Skill 切换到另一个时，应明确交接输入（如 `task_id`、文件路径、用户偏好）和预期输出，而不是假设 Skill 之间能自动编排
3. **上下文交接**：上游 Skill 完成后，将关键上下文字段（见下文各链路的交接表）传递给下游 Skill
4. **失败隔离**：一个 Skill 的规程失败不应导致整个链路无法恢复

### 9.2 典型协作链路

**链路 A：用户发送音频文件（普通模式）**

```
用户发送 .mp4 + "帮我转写"
    │
    ▼
Agent 加载 funasr-task-manager-channel-intake 规程
    │
    ├─ Layer A: 检测到文件 + 关键词 → 高置信度
    │
    ├─ 进入 funasr-task-manager-media-preflight 规程执行预检查
    │   └─ 交接输入: 文件路径
    │   └─ 交接输出: ready=true, duration=3720s, needs_conversion=true
    │
    ├─ 检查服务器: GET /api/v1/servers → 2 台 ONLINE
    │
    ├─ 参数协商: "默认 txt 格式，开始转写？" → 用户确认
    │
    ├─ 提交: upload → create_task
    │
    └─ 将上下文交接给 funasr-task-manager-result-delivery 规程
        └─ 交接输入: task_id(s), task_group_id, 用户偏好格式
        └─ 执行: 监控任务 → 拉取结果 → 返回文本 + 汇总信息
```

**链路 B：用户发送加密/敏感文件**

> ⚠ 当前后端 **尚未实现** 加密文件处理管道。链路 B 分为"当前阶段"和"未来阶段"两条路径。

**链路 B-1：当前阶段（风险拦截）**

```
用户说"这是加密的会议录音" + 发送 meeting.wav.enc
    │
    ▼
Agent 加载 funasr-task-manager-channel-intake 规程
    │
    ├─ Layer B: 检测到 "加密" + .enc 扩展名
    │
    ├─ 切换到 funasr-task-manager-secure-ingest 规程（当前仅为风险拦截）
    │   ├─ 告知用户：系统尚未支持加密文件处理管道
    │   ├─ 提供替代方案：
    │   │   1. 在本地完成转写（CLI + 本地 FunASR 服务）
    │   │   2. 等待加密功能上线后再提交
    │   │   3. 用户显式确认后以明文模式处理（需用户主动声明风险可接受）
    │   └─ 不假装已加密，不静默回退到明文
    │
    ├─ 用户选择明文处理 → 记录用户确认，进入正常 intake 流程
    └─ 用户拒绝/无响应 → 终止，不处理该文件
```

**链路 B-2：未来阶段（后端加密管道上线后）**

```
用户说"这是加密的会议录音" + 发送 meeting.wav.enc
    │
    ▼
Agent 加载 funasr-task-manager-channel-intake 规程
    │
    ├─ Layer B: 检测到 "加密" + .enc 扩展名
    │
    ├─ 切换到 funasr-task-manager-secure-ingest 规程
    │   ├─ 密钥协商
    │   ├─ 解密到受控临时目录
    │   └─ 交接输出: 解密后的临时文件路径
    │
    ├─ 进入 funasr-task-manager-media-preflight 规程（对解密后的文件）
    │
    ├─ 正常提交流程（但源文件为临时路径）
    │
    ├─ 将上下文交接给 funasr-task-manager-result-delivery 规程（结果可选加密）
    │
    └─ 回到 funasr-task-manager-secure-ingest 规程执行安全清理
```

**链路 C：外部调度触发的服务器校准**

```
外部 orchestrator（CI 定时任务 / Bot 编排器 / 运维手动触发）
唤醒 Agent 并提示 "校准服务器 benchmark"
    │
    ▼
Agent 加载 funasr-task-manager-server-benchmark 规程
    │
    ├─ CHECK: 任务队列为空 → 安全
    ├─ 遍历 ONLINE 服务器
    ├─ 逐个 benchmark
    ├─ 解读结果，写回 DB
    ├─ 与历史对比
    │   ├─ 偏差 < 20% → 静默更新
    │   └─ 偏差 > 20% → 通知运维
    └─ 记录到 4-tests/batch-testing/outputs/benchmark/
```

---

## 十、当前 API / CLI 与设计字段对齐

这部分是创建 Skill 前必须固定的项目事实。后续 `references/project-context.md` 应以此为准，并复用或同步 `funasr-task-manager-web-e2e/references/project-context.md`，避免两份项目事实漂移。

### 10.1 API 字段对齐

| 场景 | 当前后端事实 | Skill 文档约束 |
|------|-------------|---------------|
| 创建任务 | `POST /api/v1/tasks` 接收 `{"items":[{"file_id":"...","language":"zh","options":{...}}],"callback":{...}}` | 不使用旧式 `file_ids` 顶层数组；多文件通过 `items` 创建，后端生成 `task_group_id` |
| 系统负载 | `GET /api/v1/stats` 返回 `slots_used`、`queue_depth`、`slots_total` 等字段 | benchmark 前置检查使用 `slots_used > 0` 或 `queue_depth > 0` 判断忙碌 |
| 单节点 benchmark | `POST /api/v1/servers/{id}/benchmark` 返回 NDJSON | 进度事件可含 `benchmark_complete`；最终结果以 `benchmark_result` 为准 |
| 全量 benchmark | `POST /api/v1/servers/benchmark` 返回 NDJSON | 开始看 `all_benchmark_start`；单节点完成看 `server_benchmark_done`；单节点错误看 `server_error`；全部完成看 `all_complete` |
| 元数据提取 | 上传服务优先用 `ffprobe`；失败时可按文件大小估算时长 | preflight 不应把 `ffprobe` 失败等同于文件不可处理，应标记 `precise_metadata=false` |
| 结果导出 | 单任务结果支持 `txt/json/srt`；批次结果支持 `txt/json/srt/zip` | `funasr-task-manager-result-delivery` 负责按用户偏好选择格式并处理批量 zip |
| API Key 认证 | 前端流式/普通请求都不能绕过认证重试机制 | 前端相关 Skill 必须提醒复用现有 API 客户端或等价认证处理 |

### 10.2 CLI 指令对齐

> **CLI 入口说明**：开发环境推荐 `python -m cli`（在 `3-dev/src/backend/` 目录下执行）；`pip install -e .` 后可使用 `asr-cli` 替代（二者等价）。本文档统一使用 `python -m cli` 作为示例。全局参数 `--server/-s`、`--api-key/-k`、`--output/-o`、`--quiet/-q`、`--timeout` 会影响所有子命令。Skill 通过 CLI 操作时应优先使用 `--output json` 获取稳定结构化输出。

| CLI 指令 | 底层 API | 关键入参 | 输出事件/字段 | 对应 Skill |
|----------|----------|----------|---------------|------------|
| `python -m cli upload <files...>` | `POST /api/v1/files/upload` | 文件路径列表 | `file_id`、`original_name`、`size_bytes`、`status` | `funasr-task-manager-media-preflight` / `funasr-task-manager-channel-intake` |
| `python -m cli upload <files...> --create-task` | `POST /api/v1/files/upload` + `POST /api/v1/tasks` | `--language`、`--hotwords`、`--callback`、`--callback-secret` | 上传字段 + `task_id`、`task_status`；CLI 将文件 ID 映射为 API `items` | `funasr-task-manager-channel-intake` |
| `python -m cli file info <file_id>` | `GET /api/v1/files/{file_id}` | `file_id` | `original_name`、`media_type`、`mime`、`duration_sec`、`codec`、`sample_rate`、`channels`、`size_bytes`、`status` | `funasr-task-manager-media-preflight` |
| `python -m cli transcribe <file>` | `POST /api/v1/files/upload` → `POST /api/v1/tasks` → `GET /api/v1/tasks/{id}` → `GET /api/v1/tasks/{id}/result` | `--language`、`--hotwords`、`--format`、`--save`、`--callback`、`--no-wait`、`--poll-interval`、`--timeout` | 单文件输出 `file`、`task_id`、`status`、`output`；失败时非 0 退出 | `funasr-task-manager-channel-intake` + `funasr-task-manager-result-delivery` |
| `python -m cli transcribe <files...> --batch` | 多次上传 + 批量 `POST /api/v1/tasks` + `GET /api/v1/task-groups/{id}/tasks` + 单任务结果下载 | 多文件路径、`--format`、`--output-dir`、`--download/--no-download`、`--json-summary` | `task_group_id`、`task_ids`、`succeeded`、`failed`、`timeout`、`server_usage`、`results` | `funasr-task-manager-channel-intake` + `funasr-task-manager-result-delivery` |
| `python -m cli task create <file_ids...>` | `POST /api/v1/tasks` | 位置参数是 CLI 层 `file_ids`；实际 API body 是 `items[]`；支持 `--language`、`--hotwords`、`--callback`、`--wait` | `task_id`、`file_id`、`status`、`language`、`task_group_id` | `funasr-task-manager-channel-intake` |
| `python -m cli task list [--group]` | `GET /api/v1/tasks` 或 `GET /api/v1/task-groups/{id}/tasks` | `--status`、`--search`、`--group`、`--page`、`--page-size` | `items[]`、`total`；行字段含 `task_id`、`status`、`progress`、`language`、`created_at` | `funasr-task-manager-result-delivery` |
| `python -m cli task info <task_id>` | `GET /api/v1/tasks/{task_id}` | `task_id` | `task_group_id`、`status`、`progress`、`eta_seconds`、`assigned_server_id`、`retry_count`、`error_message` | `funasr-task-manager-result-delivery` |
| `python -m cli task wait <task_ids...>` / `asr-cli task wait --group <id>` | `GET /api/v1/tasks/{id}` 或 `GET /api/v1/task-groups/{id}` | `task_ids` 或 `--group`、`--poll-interval`、`--timeout` | 单任务 `status/progress`；批次 `total/succeeded/failed/canceled/progress/is_complete` | `funasr-task-manager-result-delivery` |
| `python -m cli task progress <task_id>` | `GET /api/v1/tasks/{task_id}/progress` | `task_id` | SSE 事件流，供实时进度展示 | `funasr-task-manager-result-delivery` |
| `python -m cli task result <task_id>` / `asr-cli task result --group <id>` | `GET /api/v1/tasks/{id}/result` 或 `GET /api/v1/task-groups/{id}/results` | `--format json/txt/srt`；批次支持逗号分隔多格式；`--save`、`--output-dir` | 单任务返回结果内容；批次写出多格式文件并生成 `batch-summary.json` | `funasr-task-manager-result-delivery` |
| `python -m cli task cancel <task_id>` / `asr-cli task delete --group <id>` | `POST /api/v1/tasks/{id}/cancel` 或 `DELETE /api/v1/task-groups/{id}` | `task_id` 或 `--group` | 取消后的任务对象；批次删除返回 `deleted`、`skipped_active` | `funasr-task-manager-result-delivery` |
| `python -m cli server list` | `GET /api/v1/servers` | 无 | `server_id`、`host`、`port`、`status`、`max_concurrency`、`rtf_baseline`、`throughput_rtf`、`benchmark_concurrency` | `funasr-task-manager-server-benchmark` |
| `python -m cli server register --id ... --host ... --port ... [--benchmark]` | `POST /api/v1/servers` | `--id`、`--name`、`--host`、`--port`、`--protocol`、`--max-concurrency`、`--benchmark` | 普通注册返回 server 对象；带 `--benchmark` 时流式事件含 `server_registered`、`benchmark_result`、`benchmark_error` | `funasr-task-manager-server-benchmark` |
| `python -m cli server probe <server_id>` | `POST /api/v1/servers/{id}/probe` | `--level connect_only/offline_light/twopass_full` | `reachable`、`responsive`、`inferred_server_type`、`supports_offline`、`supports_2pass`、`supports_online`、`probe_duration_ms` | `funasr-task-manager-server-benchmark` |
| `python -m cli server benchmark [server_id]` | `POST /api/v1/servers/{id}/benchmark` 或 `POST /api/v1/servers/benchmark` | 可选 `server_id` | 单节点事件：`benchmark_start`、`phase_*`、`gradient_*`、`benchmark_result`、`benchmark_error`；全量事件：`all_benchmark_start`、`server_benchmark_done`、`server_error`、`all_complete` | `funasr-task-manager-server-benchmark` |
| `python -m cli server update <server_id>` / `asr-cli server delete <server_id>` | `PATCH /api/v1/servers/{id}` 或 `DELETE /api/v1/servers/{id}` | `--name`、`--host`、`--port`、`--max-concurrency`、`--protocol` | 更新后 server 对象；删除成功信息 | `funasr-task-manager-server-benchmark` |
| `python -m cli stats` | `GET /api/v1/stats` | 无 | `server_online/server_total`、`slots_used/slots_total`、`queue_depth`、`tasks_today_completed`、`tasks_today_failed`、`success_rate_24h`、`avg_rtf` | `funasr-task-manager-server-benchmark` |
| `python -m cli health` / `doctor` / `metrics` | `/health`、`GET /api/v1/diagnostics`、`/metrics` | 无 | 健康状态、依赖检查、Prometheus 原始指标 | `funasr-task-manager-media-preflight` / `funasr-task-manager-server-benchmark` |
| `python -m cli config set/get/list` | 本地配置文件（`~/.asr-cli.yaml`），无后端 API | `server`、`api_key`、`output` | 当前 CLI 配置值；影响全局 `--server`、`--api-key`、`--output` 默认值 | 所有需要 CLI 的 Skill |

---

## 十一、Skill 验收标准

每个新 Skill 创建后，至少要用以下样例完成人工或脚本化验收：

| Skill | 触发样例（至少 3 个） | 不触发样例（至少 3 个） | 失败路径样例（至少 1 个） |
|-------|----------------------|-------------------------|---------------------------|
| `funasr-task-manager-channel-intake` | 用户发 `.wav`；用户说"帮我转写"；用户发多个 `.mp4` 并说"批量处理" | 纯闲聊；图片文件；PDF 且未提转写 | 有意图但无文件，必须提示上传文件 |
| `funasr-task-manager-result-delivery` | 提供 `task_id` 要结果；提供 `task_group_id` 要 zip；要求重新导出 srt | 新文件上传请求；benchmark 请求；普通服务器注册请求 | 任务失败时返回失败原因，不生成空结果 |
| `funasr-task-manager-media-preflight` | 用户问文件能否转写；intake 提交前检查；排查时长不准 | benchmark 请求；服务器调度讨论；无文件的闲聊 | `ffprobe` 不可用时返回估算和 warning，不直接中断 |
| `funasr-task-manager-server-benchmark` | 用户说跑 benchmark；新服务器注册后手动校准；外部 orchestrator 触发闲时校准 | 用户实时转写请求；队列忙且未授权；普通结果导出 | `slots_used > 0` 或 `queue_depth > 0` 时外部调度触发必须放弃 |
| `funasr-task-manager-secure-ingest` | 用户说保密；文件扩展名 `.enc`；用户要求加密返回结果 | 普通公开音频；无敏感关键词；测试素材 | 后端无加密管道时必须拦截并说明风险，不假装安全处理 |
| `funasr-task-manager-frontend-audio-intake` | 用户要求开发上传入口；修改前端音频入口；排查上传认证问题 | 普通转写请求；benchmark 请求；文档归档请求 | 流式请求 401 时必须提醒不要绕过 API Key 重试 |

### 11.1 结构化评估样例格式（evals）

建议在每个 Skill 目录下创建 `evals/evals.json`，用于脚本化验收和持续回归。格式如下：

```json
{
  "skill": "funasr-task-manager-channel-intake",
  "version": "1.0",
  "evals": [
    {
      "id": "trigger-wav-upload",
      "type": "should_trigger",
      "prompt": "用户发了一个 meeting-notes.wav 文件，说「帮我把这个转成文字」",
      "expected_skill": "funasr-task-manager-channel-intake",
      "expected_behavior": "识别转写意图，进入 intake 规程",
      "assert_contains": ["转写", "格式", "确认提交"]
    },
    {
      "id": "no-trigger-image",
      "type": "should_not_trigger",
      "prompt": "用户发了一个 screenshot.png，说「看看这个图片」",
      "expected_skill": null,
      "reason": "图片文件不属于音视频，不应触发 intake"
    },
    {
      "id": "fail-no-file",
      "type": "failure_path",
      "prompt": "用户说「帮我转写一下」，但没有附带任何文件",
      "expected_behavior": "提示用户上传文件，不创建空任务",
      "assert_not_contains": ["任务已创建", "task_id"]
    }
  ]
}
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `type` | `should_trigger` / `should_not_trigger` / `failure_path` |
| `prompt` | 模拟用户输入（真实 prompt） |
| `expected_skill` | 期望被触发的 Skill，`null` 表示不应触发 |
| `expected_behavior` | 期望的 Agent 行为描述 |
| `assert_contains` | Agent 回复中必须包含的关键词列表 |
| `assert_not_contains` | Agent 回复中不应出现的关键词列表 |

---

## 十二、实施路线图

### 12.1 第一阶段（P0）：跑通基本闭环 ✅ 已完成

**目标**：Agent 能看到音频文件 → 预检查 → 提交任务 → 返回结果

| 顺序 | Skill | 产出 | 状态 |
|------|-------|------|------|
| 1 | `funasr-task-manager-media-preflight` | SKILL.md + evals/ + references/ | ✅ 已完成 |
| 2 | `funasr-task-manager-channel-intake` | SKILL.md + evals/ + references/ | ✅ 已完成 |
| 3 | `funasr-task-manager-result-delivery` | SKILL.md + evals/ + references/ | ✅ 已完成 |

**里程碑**：Agent 能自主完成"用户发文件 → 转写 → 返回结果"的完整流程。

### 12.2 第二阶段（P1）：调度基础设施可信 ✅ 已完成

**目标**：Benchmark 可由 Agent（经外部 orchestrator 唤醒或用户显式请求）安全发起，调度基线持续可信

| 顺序 | Skill | 产出 | 状态 |
|------|-------|------|------|
| 4 | `funasr-task-manager-server-benchmark` | SKILL.md + evals/ + references/ | ✅ 已完成 |

**里程碑**：外部调度器可在闲时唤醒 Agent 校准 benchmark，测试前能确保基线可信。

### 12.3 第三阶段（P2）：安全能力

**目标**：Agent 能识别敏感场景并切换到安全流程

| 顺序 | Skill | 产出 | 预计工作量 |
|------|-------|------|----------|
| 5 | `funasr-task-manager-secure-ingest` | SKILL.md（流程约束层） | 1 天 |
| 6 | 后端加密管道实现 | crypto.py + secure_file_manager.py + API 扩展 | 5-8 天 |

**里程碑**：Agent 能正确引导用户进入安全模式，后端支持加密文件全生命周期管理。

### 12.4 持续迭代

| 方向 | 内容 |
|------|------|
| `funasr-task-manager-channel-intake` 扩展 | 支持更多 channel 类型、更精准的意图识别 |
| `funasr-task-manager-result-delivery` 扩展 | 重新导出、质量复核、结果补发、批量归档 |
| `funasr-task-manager-server-benchmark` 扩展 | 长期性能趋势分析、自动扩缩容建议 |
| `funasr-task-manager-media-preflight` 扩展 | 更多格式支持、音频质量评估（信噪比等） |
| 前端开发辅助 Skill | 项目专用 frontend skill，记录 Vue + Element Plus 约定 |
| 结果后处理 Skill | 翻译、摘要、关键词提取等增值功能 |

---

## 十三、Skill 命名规范

本项目内的项目专属 Skill 统一使用 `funasr-task-manager-` 前缀。

**命名格式：**

```
funasr-task-manager-{domain}-{action}
```

**规则：**

| 规则 | 说明 |
|------|------|
| 前缀 | 依赖本项目 API、CLI、目录结构、测试工件路径的 Skill，**必须**使用 `funasr-task-manager-` 前缀 |
| 通用前缀 | 只有可脱离本仓库、适用于任意 FunASR 项目的通用 Skill，才使用 `funasr-` 前缀 |
| 字符集 | 名称使用小写字母、数字和连字符 |
| 语义 | 名称优先表达触发场景和职责边界，不使用缩写（`ftm-`、`asr-`、`tm-` 等均不可） |
| 长度 | 不超过 50 字符（含前缀），当 `media` 等修饰词在上下文中已无歧义时可省略（如 `secure-ingest` 而非 `secure-media-ingest`） |

**当前 Skill 命名体系：**

| 分类 | Skill 名称 |
|------|-----------|
| 核心转写流程（P0） | `funasr-task-manager-channel-intake` |
|  | `funasr-task-manager-result-delivery` |
|  | `funasr-task-manager-media-preflight` |
| 基础设施与测试（P1） | `funasr-task-manager-server-benchmark` |
|  | `funasr-task-manager-reset-test-db`（建议更名，需评估对既有引用的影响） |
|  | `funasr-task-manager-web-e2e`（已有，保持不变） |
| 安全与前端（P2+） | `funasr-task-manager-secure-ingest` |
|  | `funasr-task-manager-frontend-audio-intake` |

**不采用的命名方式：**

| 前缀 | 不采用原因 |
|------|-----------|
| `funasr-*` | 太泛，除非 Skill 可以离开本项目复用 |
| `asr-*` | 太泛，和其他 ASR 系统容易混淆 |
| `ftm-*` | 短但不直观，触发和人工识别都差 |
| `task-manager-*` | 缺少 FunASR 上下文 |

> 注：原 `reset-asr-db-before-test` 已更名为 `funasr-task-manager-reset-test-db`，目录和 SKILL.md 内部引用已同步更新。

---

## 十四、目录结构规划

```
6-skills/
├── funasr-task-manager-web-e2e/       # ✅ 已有
│   ├── SKILL.md
│   ├── agents/
│   ├── references/
│   └── scripts/
├── funasr-task-manager-reset-test-db/  # ✅ 已有（已从 funasr-task-manager-reset-test-db 更名）
│   ├── SKILL.md
│   └── scripts/
├── funasr-task-manager-channel-intake/             # ✅ 已建（P0）
│   ├── SKILL.md
│   ├── evals/
│   │   └── evals.json                 # 结构化评估样例（见 11.1）
│   └── references/
│       ├── project-context.md         # 复用/同步 funasr-task-manager-web-e2e/references/project-context.md
│       ├── trigger-keywords.json      # 触发关键词与权重
│       └── response-templates.md      # 用户交互模板
├── funasr-task-manager-result-delivery/            # ✅ 已建（P0）
│   ├── SKILL.md
│   ├── evals/
│   │   └── evals.json
│   └── references/
│       ├── result-formats.md          # txt/json/srt/zip 导出规则
│       ├── quality-checklist.md       # 空文本、乱码、异常短文本检查
│       └── response-templates.md      # 结果回传模板
├── funasr-task-manager-media-preflight/            # ✅ 已建（P0）
│   ├── SKILL.md
│   ├── evals/
│   │   └── evals.json
│   └── references/
│       ├── supported-formats.json     # 支持的格式列表
│       └── conversion-rules.md        # 转码规则与条件
├── funasr-task-manager-server-benchmark/           # ✅ 已建（P1）
│   ├── SKILL.md
│   ├── evals/
│   │   └── evals.json
│   └── references/
│       ├── project-context.md         # benchmark 相关 API/CLI 端点与权限
│       ├── safety-checklist.md        # 执行前安全检查清单
│       ├── ndjson-events.md           # NDJSON 事件格式与解读
│       └── result-templates.md        # 结果解读与汇报模板
├── funasr-task-manager-secure-ingest/        # 🆕 待建（P2）
│   ├── SKILL.md
│   ├── agents/
│   ├── evals/
│   │   └── evals.json
│   └── references/
│       ├── threat-model.md            # 威胁模型与暴露面分析
│       ├── security-non-goals.md      # 安全模式不承诺解决的问题
│       └── encryption-protocols.md    # 支持的加密方式与密钥管理
└── funasr-task-manager-frontend-audio-intake/      # 预留，暂不创建
    ├── SKILL.md
    ├── agents/
    ├── evals/
    │   └── evals.json
    └── references/
        ├── frontend-api-contract.md   # Vue + Element Plus + API Key 认证约束
        └── e2e-acceptance.md          # 前端入口验收路径
```

---

## 十五、设计决策记录

### D1：为什么入口采用 `funasr-task-manager-channel-intake` + `funasr-task-manager-result-delivery`？

曾考虑把"入口、提交、监控、交付、质量初筛"全部放进一个 `funasr-task-intake`。最终改为入口和出口拆分，理由：

1. **入口职责更清楚**：`funasr-task-manager-channel-intake` 只回答"该不该启动 ASR、用什么参数提交"，不承担长期轮询和结果复核
2. **结果交付可独立复用**：已有任务完成后补发结果、重新导出 srt/json、批量质量复核，都不应从 intake 开始
3. **MVP 可聚合实现**：第一版可以在 `funasr-task-manager-channel-intake` 临时内置交付逻辑，但 Skill 边界必须保留，后续拆分不改用户心智

### D2：为什么 `funasr-task-manager-media-preflight` 要独立成 Skill？

1. **复用性**：不只被 intake 调用，也可以独立用于排查文件问题
2. **职责单一**：文件级检查是纯技术判断，不涉及用户交互决策
3. **安全模式兼容**：在 `funasr-task-manager-secure-ingest` 流程中，preflight 只能检查解密后的临时文件，需要明确的调用边界

### D3：为什么 `funasr-task-manager-server-benchmark` 的核心约束是"不能随便跑"？

Benchmark 会发送真实音频到服务器并占用计算资源。如果在用户转写高峰期跑 benchmark：
- 服务器资源被 benchmark 占用，真实任务延迟增加
- Benchmark 结果本身也不准确（因为与真实任务竞争资源）
- 用户体验恶化

所以 **空闲检查是执行 benchmark 的硬性前提**，不是可选步骤。

### D4：为什么 `funasr-task-manager-secure-ingest` 分两层设计？

1. **Skill 层可以先落地**：即使后端还没有加密管道，Skill 也可以指导 Agent 正确引导用户（如建议本地处理、明确告知风险）
2. **避免假装安全**：如果只有 Skill 没有后端能力，Agent 不能假装已加密处理——这比没有安全功能更危险
3. **后端能力可以独立开发**：`crypto.py` 等模块的开发不依赖 Skill 设计

### D5：为什么前端开发辅助 Skill 先预留、不创建？

另一位架构师建议做一个 `funasr-task-manager-frontend-audio-intake` Skill，记录 Vue + Element Plus 的项目约定，帮助 Agent 写前端代码时少走弯路。

当前阶段先预留、不创建，理由：
1. 已有的 `funasr-task-manager-web-e2e` 的 `references/project-context.md` 已经记录了关键前端约定
2. 前端代码的约定更适合放在 `AGENTS.md` 或 `.cursor/rules/` 中，而非独立 Skill
3. 如果开始让 Agent 实际开发前端音频入口，再把 `funasr-task-manager-frontend-audio-intake` 建成专用 Skill，明确 Vue + Element Plus、API Key 认证、上传/任务接口和 E2E 验收路径

---

## 十六、附录

### A. 后端 API 端点速查

| 方法 | 路径 | 说明 | Skill 使用 |
|------|------|------|-----------|
| GET | `/health` | 健康检查 | channel-intake（Phase 2） |
| POST | `/api/v1/files/upload` | 上传文件 | channel-intake（Phase 4） |
| GET | `/api/v1/files/{file_id}` | 文件元数据 | preflight |
| POST | `/api/v1/tasks` | 批量创建任务 | channel-intake（Phase 4） |
| GET | `/api/v1/tasks` | 任务列表 | result-delivery |
| GET | `/api/v1/tasks/{task_id}` | 任务详情 | result-delivery |
| GET | `/api/v1/tasks/{task_id}/result` | 结果（json/txt/srt） | result-delivery |
| GET | `/api/v1/tasks/{task_id}/progress` | SSE 进度流 | result-delivery |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 | result-delivery（错误处理） |
| GET | `/api/v1/task-groups/{group_id}` | 批次汇总 | result-delivery |
| GET | `/api/v1/task-groups/{group_id}/results` | 批次结果（txt/json/srt/zip） | result-delivery |
| GET | `/api/v1/servers` | 服务器列表 | channel-intake（Phase 2）/ benchmark |
| POST | `/api/v1/servers` | 注册服务器 | benchmark |
| POST | `/api/v1/servers/{id}/benchmark` | 单节点 benchmark（NDJSON） | benchmark |
| POST | `/api/v1/servers/benchmark` | 全量 benchmark（NDJSON） | benchmark |
| POST | `/api/v1/servers/{id}/probe` | 连通性探测 | benchmark |
| GET | `/api/v1/stats` | 系统统计 | benchmark（前置检查） |
| GET | `/api/v1/diagnostics` | 系统诊断 | preflight（ffprobe 可用性） |

### B. 任务状态机

```
PENDING → PREPROCESSING → QUEUED → DISPATCHED → TRANSCRIBING → SUCCEEDED
                                                              → FAILED → QUEUED（重试）
    ↓         ↓              ↓         ↓             ↓
  CANCELED  CANCELED/FAILED  CANCELED  CANCELED     CANCELED
```

| 状态 | 含义 | Agent 对用户的描述 |
|------|------|------------------|
| PENDING | 已创建 | "任务已创建" |
| PREPROCESSING | 预处理中 | "文件预处理中（转码等）..." |
| QUEUED | 排队中 | "等待可用服务器..." |
| DISPATCHED | 已分配 | "已分配到服务器，即将开始" |
| TRANSCRIBING | 转写中 | "正在转写..." |
| SUCCEEDED | 成功 | "转写完成！" |
| FAILED | 失败 | "转写失败：{原因}" |
| CANCELED | 已取消 | "任务已取消" |

### C. 支持的音视频格式

与后端 `app/storage/file_manager.py` 保持一致：

```
音频: .wav .mp3 .flac .ogg .m4a .aac .wma .pcm
视频: .mp4 .webm .mkv .avi .mov
```

### D. 服务器状态

| 状态 | 含义 | Agent 行为 |
|------|------|-----------|
| ONLINE | 在线，可接收任务 | 正常提交 |
| OFFLINE | 离线 | 不提交到该服务器 |
| DEGRADED | 降级（性能下降） | 可提交但提醒用户可能较慢 |
