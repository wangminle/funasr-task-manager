# ASR 转写工作流操作手册

> 本文件由 `funasr-task-manager-init` Skill Phase 6 安装到 Agent workspace，供 Agent 随时检索 ASR 转写相关知识。
> 源文件位于仓库 `6-skills/_shared/ASR-WORKFLOW.md`，更新后重新执行 Phase 6 即可同步。

---

> **实时通知规范（强制）**：所有阶段通知必须通过 `send_user_notice()` 实时推送，禁止仅输出普通 assistant 文本。详见 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。
>
> - **OpenClaw/飞书环境**：优先调用 runtime 暴露的 `message` tool（`{"name": "message", "arguments": {"action": "send", "message": "..."}}`）
> - **无 message tool 时**：调用 `python -m cli notify send --text "..."`
> - **普通文本**：仅当运行在纯本地终端且用户直接看到实时输出时可用
>
> 背景：普通 assistant 文本在 OpenClaw/Hermes 等平台中被 turn 级缓冲，turn 结束后才推送到飞书，导致用户在处理期间看不到进度。`send_user_notice()` 通过工具调用副作用绕过缓冲，实现即时送达。

---

## 执行流程（5 阶段）

收到用户消息后，按以下阶段顺序执行。**每个阶段至少通过 `send_user_notice()` 发一条状态通知，禁止静默执行。**

### Phase 1：意图确认

- 检测消息中是否包含音视频文件或 ASR 关键词（转写/识别/字幕/ASR/transcribe）
- **有文件 + 关键词** → 直接执行；**有文件无关键词** → 主动询问"是否需要转写？"
- 确认用户意图后进入下一阶段

### Phase 1.5：渠道文件下载

- 从渠道 API 下载用户发送的文件到本地 `~/media/inbound/` 或 `uploads/`
- 飞书文件 >50MB 时返回错误码 `234037`，需自动切换 HTTP Range 分块下载（10MB/块）
- 下载完成后通知用户："✅ 文件已下载（{size}MB），开始预检..."

### Phase 2：预检查

- 运行 `ffprobe` 验证文件格式、时长、编码、采样率
- 非音视频格式 → 拒绝并告知用户
- 需要转码的格式 → ffmpeg 转为 16kHz 单声道 WAV
- 检查后端是否可达（`curl -sf http://127.0.0.1:15797/health`），不可达时按优先级尝试：
  1. `systemctl --user start funasr-task-manager-backend`（如已配置用户级 systemd 服务，无需 sudo）
  2. `cd {ASR_PROJECT_ROOT}/3-dev/src/backend && nohup uvicorn app.main:app --host 0.0.0.0 --port 15797 &`（降级方案）

### Phase 3：参数协商与任务提交

- 根据音频时长自动选择分段策略（详见下方 [音频分段策略](#音频分段策略)）
- 通过 `/api/v1/files/upload` 上传文件
- 通过 `/api/v1/tasks` 创建转写任务
- 通知用户："⏳ 任务已提交（ID: {task_id}），预计 {estimate} 完成"

### Phase 4：转写监控

- 轮询 `/api/v1/tasks/{id}` 状态，或通过 SSE `/api/v1/tasks/{id}/progress` 实时监听
- 长时间无进展时主动告知用户当前状态
- 任务失败时展示错误原因并建议重试方案

### Phase 5：结果交付

- 转写完成后**必须主动通知用户**，不可等用户询问
- 短文本（<2000 字）：直接发送到对话
- 长文本（>=2000 字）：上传为 **txt 文件附件** 发送，不粘贴全文
- 飞书发消息必须带 `receive_id_type=chat_id` 参数

---

## 参考知识

### 转写核心流程概览

```
用户发起 → 意图识别 → 文件获取 → 媒体预检 → 转写执行 → 结果交付
```

| 步骤 | 负责 Skill | 关键动作 |
|------|-----------|---------|
| 意图识别 | channel-intake | 识别用户消息中的音视频文件或 ASR 关键词 |
| 文件获取 | channel-intake | 从渠道 API 下载文件（飞书 >50MB 需分块下载） |
| 本地批量扫描 | local-batch-transcribe | 扫描本地目录，建 manifest 清单，分 chunk 提交 |
| 媒体预检 | media-preflight | ffprobe 验证格式/时长/编码，决定是否转码 |
| 转写执行 | 后端自动 | 调用 FunASR 服务器集群，长音频自动 VAD 分段并行 |
| 进度监控 | **batch-monitor**（子 Agent） | 子 Agent 循环查询 task-group status，通过 message tool 播报进度 |
| 结果交付 | result-delivery | 轮询任务状态，完成后格式化结果通知用户 |

### 音频分段策略

当音频时长超过触发阈值时，后端自动 VAD 分段并行转写：

| 档位 | 目标时长 | 触发阈值 | 搜索步长 |
|------|---------|---------|---------|
| 10m | 600s | 720s（12分钟） | 60s |
| 20m | 1200s | 1440s（24分钟） | 120s |
| 30m | 1800s | 2160s（36分钟） | 180s |

- 切分算法：双向交替搜索（后→前→后），在 VAD 静音点切割
- 重叠：400ms overlap 避免边界丢词
- 分段独立调度到不同服务器，全部完成后合并

### 服务器调度

调度算法（按优先级）：
1. **LPT（最长处理时间优先）** — 长音频优先分配到快节点
2. **EFT（最早完成时间）** — 选预计最早空闲的节点
3. **Work Stealing** — 空闲节点从忙碌节点队列偷任务
4. **运行时 RTF 校准** — 根据实际转写速度动态调整节点权重

### 任务状态流转

```
PENDING → PREPROCESSING → QUEUED → DISPATCHED → TRANSCRIBING → SUCCEEDED
                                                              → FAILED
                                              → CANCELED
```

长音频在 PREPROCESSING 阶段完成 VAD 分段，segment 独立调度，父任务状态对外不变。

### 文件格式支持

允许：`.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm`

- 免转码：`.wav`、`.pcm`（直接发给 FunASR）
- 需转码：其他格式（ffmpeg → 16kHz 单声道 WAV）

### 关键 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/files/upload` | 上传文件 |
| POST | `/api/v1/tasks` | 创建转写任务 |
| GET | `/api/v1/tasks/{id}` | 任务状态 |
| GET | `/api/v1/tasks/{id}/result` | 转写结果 |
| GET | `/api/v1/tasks/{id}/progress` | SSE 实时进度 |
| GET | `/api/v1/task-groups/{id}` | 任务组聚合统计（子 Agent 播报用） |
| GET | `/api/v1/task-groups/{id}/tasks` | 任务组内任务列表 |
| GET | `/api/v1/task-groups/{id}/results` | 任务组结果批量下载 |
| GET | `/api/v1/servers` | 服务器列表（Admin） |

### task-group CLI 短命令

子 Agent 监控模式使用以下短命令，每条秒级返回：

| 命令 | 用途 | 调用方 |
|------|------|--------|
| `python -m cli --output json task-group scan {dir}` | 扫描目录 → JSON 清单 | 主 Agent |
| `python -m cli --output json task-group submit --manifest {file}` | 提交 → task_group_id | 主 Agent |
| `python -m cli --output json task-group status {group_id}` | 查询进度 → JSON | 子 Agent |
| `python -m cli --output json task-group download {group_id}` | 下载结果 → 路径 | 子 Agent |

### Skill 协作链

本项目有两条主要转写入口，共享同一后端和结果交付能力：

**入口 A：渠道实时转写（channel-intake）**

用户在聊天中发送音视频文件，逐个处理：

```
init → channel-intake → media-preflight → [后端转写] → result-delivery
         ↑                                                    ↓
      用户发起                                           通知用户结果
```

**入口 B：服务器本地批量转写（local-batch-transcribe + batch-monitor）**

用户指令扫描服务器本地目录，批量处理。采用**异步调度架构**：主 Agent 负责扫描和提交，子 Agent 负责监控和播报。

```
init → local-batch-transcribe（主 Agent）
         │
         ├─ Phase 1-2：扫描目录、建清单（task-group scan）
         ├─ Phase 3：media-preflight（批量预检）
         ├─ Phase 4：task-group submit 批量提交
         ├─ Phase 5：委托子 Agent 执行 batch-monitor
         │            │
         │            └─ 主 Agent 释放，继续接新任务
         │
         └─ batch-monitor（子 Agent）
              ├─ 定期 task-group status 查询进度
              ├─ 通过 message tool 播报进度
              ├─ 全部完成 → task-group download 下载结果
              └─ 发送完成汇总 → 退出
```

> **为什么拆成两个 Agent**：批量任务可能持续数分钟到数十分钟。如果主 Agent 自己轮询，就会被长任务绑死，无法响应群聊中其他用户的消息。委托子 Agent 监控后，主 Agent 秒级释放，可以同时处理多个用户的请求。

**协作规则**：当两个入口同时有任务时，channel-intake 优先——local-batch-transcribe 暂停新提交、等待 intake 完成后恢复（让步机制）。

**触发条件对比**：

| 场景 | 触发的 Skill |
|------|-------------|
| 用户在聊天发送 1 个音频文件 | `channel-intake` |
| 用户说"帮我转写 inbox 里的文件" | `local-batch-transcribe` |
| 用户说"批量转写 /data/audio/" | `local-batch-transcribe` |
| 用户说"重试失败项" | `local-batch-transcribe` |
| 用户说"继续上次的批量转写" | `local-batch-transcribe` |

辅助 Skills：
- `batch-monitor` — 子 Agent 异步监控播报（绑定 task_group_id，定期查询并发通知）
- `server-benchmark` — 性能测试与 RTF 校准
- `reset-test-db` — 重置本地测试环境
- `web-e2e` — 浏览器端到端测试

### 常见问题速查

| 问题 | 原因 | 解决 |
|------|------|------|
| 转写卡在 DISPATCHED | 所有服务器 OFFLINE | 检查 FunASR Docker 容器 |
| 飞书下载失败 234037 | 文件 >50MB | 自动切换 Range 分块下载 |
| ffprobe 格式不识别 | 文件损坏或非音视频 | media-preflight 会拒绝 |
| 数据库迁移失败 | alembic 版本不一致 | `alembic downgrade base && upgrade head` |
| systemd 服务启动失败 | Python 路径无 uvicorn | 检查 service 文件中的 ExecStart 路径 |
