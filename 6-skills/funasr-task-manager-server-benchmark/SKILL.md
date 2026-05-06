---
name: funasr-task-manager-server-benchmark
description: >
  Safely benchmark FunASR servers and calibrate scheduling baselines.
  Use when: user explicitly requests benchmark or performance test,
  external orchestrator triggers idle-time calibration, or user asks
  to calibrate a newly registered server. NOT for passive use during
  normal transcription — missing rtf_baseline should use default RTF.
---

# 服务器能力校准

Benchmark 是调度准确性的基础设施。它写回三个关键字段，直接影响调度器的 ETA 预估和配额分配：

- **`rtf_baseline`**（= benchmark 的 `single_rtf`）：直接参与调度器 ETA 预估（`get_effective_rtf()` 使用生产 P90 或 `rtf_baseline` 回退值）和配额分配（`get_throughput_speed()` = `max_concurrency / base_rtf`）
- **`max_concurrency`**（= benchmark 的 `recommended_concurrency`）：决定服务器可用 slot 数量，直接影响并发调度
- **`throughput_rtf`**：并发吞吐量基准，当前主要作为 benchmark 记录和容量对比指标（`capacity_comparison`），**不直接参与调度计算**

本 Skill 不只是"发起 benchmark"——它是 **安全发起 → 实时解读 → 写回校准 → 归档记录** 的完整规程。

## 触发条件

### 外部调度触发（需 orchestrator 唤醒 Agent）

- 外部 orchestrator（CI / cron / Bot）在低峰时段唤醒 Agent 执行校准
- 新服务器注册后，orchestrator 或用户指示校准
- 距上次 benchmark 超过设定周期（由外部调度判断，非 Skill 自行检测）
- 调度器 ETA 预估偏差持续 > 30%，运维或监控系统触发校准

### 被动触发（用户/开发者显式请求）

- 用户说"跑一下 benchmark"/"测试一下服务器性能"/"校准一下"
- 测试前需要确保调度基线可信
- 注册服务器时选择了 `--benchmark` 参数

### 关键词

中文：`benchmark` / `测速` / `性能测试` / `基准测试` / `校准` / `基线`
英文：`benchmark` / `calibrate` / `perf test` / `RTF` / `throughput`

### 不触发

- 用户正在提交实时转写任务（不阻塞用户转写去跑 benchmark）
- 纯闲聊、结果导出、文件预检查等无关场景
- 无 admin 权限时（benchmark 端点需要 AdminUser）

> **核心约束**：如果用户正在提交转写任务且服务器只是缺少 `rtf_baseline`，此 Skill 不应自动阻塞用户任务。应使用默认基线估算（RTF=0.3）并提示"性能基线未校准"，只有用户确认或进入测试/闲时校准场景时才执行。

## 安全约束——Benchmark 不能随便跑

Benchmark 本身会向服务器发送真实音频并占用计算资源。如果在高负载时发起，会干扰正在执行的转写任务。

详细安全检查清单见 `references/safety-checklist.md`，以下是概要：

### CHECK 1：任务队列状态

- `GET /api/v1/stats` → 检查 `slots_used` / `queue_depth`
- `slots_used > 0` 或 `queue_depth > 0` →
  - 用户请求场景：警告并等待用户确认
  - 外部调度触发：直接放弃，报告"队列非空，跳过校准"
- 队列为空 → 安全，继续

### CHECK 2：目标服务器状态

- `GET /api/v1/servers`（需 admin token）→ 检查目标服务器 status
  - 或用 `GET /api/v1/stats` 的 `server_online` 做粗粒度判断（无需 admin）
- `OFFLINE` → 先 probe（`POST /api/v1/servers/{id}/probe`），不直接 benchmark
- `DEGRADED` → 警告"服务器处于降级状态，benchmark 结果可能不准确"
- `ONLINE` → 继续

### CHECK 3：距上次 benchmark 的间隔

- 上次 benchmark < 10 分钟 → 跳过，提示"刚跑过，无需重复"
- 否则 → 继续

**权限要求**：benchmark 端点（`POST /api/v1/servers/{id}/benchmark` 和 `POST /api/v1/servers/benchmark`）以及服务器列表端点（`GET /api/v1/servers`）均需要 AdminUser 认证。执行 benchmark 的 Agent 必须持有 admin token。

## 执行流程

### Phase 1：前置检查

按上述安全约束依次执行 CHECK 1-3。任一检查不通过时，按场景决定放弃或等待确认。

### Phase 2：选择 Benchmark 范围

三种模式：

| 模式 | 端点 | 适用场景 | 执行方式 |
|------|------|---------|---------|
| 单服务器 | `POST /api/v1/servers/{server_id}/benchmark` | 指定服务器校准 | 单次调用 |
| 安全校准（逐个顺序） | 循环调用 `POST /api/v1/servers/{id}/benchmark` | 闲时校准、外部调度 | 逐个顺序执行 |
| 全量并发压测 | `POST /api/v1/servers/benchmark` | 用户明确请求"全量压测" | 并发执行 |

**关键区分**：

- **安全校准**应使用逐个循环调用单节点端点，避免同时压满多台服务器
- **全量并发压测**（`POST /api/v1/servers/benchmark`）会为所有 ONLINE 节点创建并发 benchmark 任务，**仅在用户明确请求"全量压测"时使用**
- 闲时校准场景**禁止**使用全量并发端点

### Phase 3：实时进度解读

> **实时通知规范**：本 Skill 的所有用户通知必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。禁止用普通文本替代 `send_user_notice()`。

解析 NDJSON 流式事件，**通过 `send_user_notice()` 立即向用户推送进度**。

事件详情见 `references/ndjson-events.md`，以下事件必须触发 `send_user_notice()`：

- `benchmark_start` → `send_user_notice("开始 benchmark，共 2 阶段")`
- `phase_start` → `send_user_notice("Phase {phase}: {description}...")`
- `phase_progress` → 静默（除非用户要求详细日志）
- `phase_complete` → `send_user_notice("Phase 1 完成: 单线程 RTF = {single_rtf}")`
- `gradient_start` → 静默（频率控制：只在首个和末个 gradient 通知）
- `gradient_complete` → `send_user_notice("N={concurrency}: throughput_rtf = {throughput_rtf}, wall={wall_clock_sec}s")`
- `gradient_error` → `send_user_notice("⚠️ N={concurrency}: {error}")`
- `benchmark_complete` → 服务层完成（Agent 可忽略，等待 API 终结事件）
- `benchmark_result` → `send_user_notice("✅ 节点 {server_name} benchmark 完成：推荐并发数 {recommended_concurrency}")`
- `all_complete` → `send_user_notice("✅ 全量 benchmark 完成：{summary}")`

**OpenClaw 环境调用示例：**

```json
{"name": "message", "arguments": {"action": "send", "message": "Phase 1: 单线程性能测量..."}}
```

**CLI fallback：**

```bash
python -m cli notify send --text "Phase 1: 单线程性能测量..."
```

### Phase 4：结果解读与校准

- 解读 `single_rtf`（单线程处理速度，越低越快）
- 解读 `throughput_rtf`（并发吞吐量，越低越快）
- 解读 `recommended_concurrency`（推荐并发数）
- 检测退化（某梯度级别性能下降 > 10%）时解释可能原因：
  - 服务器资源不足（CPU/GPU 瓶颈）
  - 网络延迟（LAN vs WAN 差异）
  - 模型加载慢
  - 建议：降低 max_concurrency / 检查网络 / 检查 GPU 显存
- 调度影响说明（基于结果解读模板，见 `references/result-templates.md`）
- 自动写回 DB（benchmark 接口已内置，无需额外操作）

### Phase 5：记录与归档

- 生成 benchmark 报告（JSON 格式）
- 保存到 `4-tests/batch-testing/outputs/benchmark/`
  - 文件名格式：`benchmark-{server_id}-{YYYYMMDD-HHmmss}.json`
  - 此目录是 benchmark 历史数据的唯一可靠来源（后端无 `last_benchmark_at` 字段）
- 与历史归档文件对比（如有），检测长期性能趋势
- 安全校准场景：如果 RTF 偏差 > 20% →
  - **当前**：在报告中标注"⚠ 性能异常"并通知运维（文字告警）
  - **不可执行**：后端 `ServerUpdateRequest` 当前不支持更新 `status` 字段，因此 Agent 不能通过 API 标记服务器为 DEGRADED。如果未来后端补充状态更新接口，可在此补充自动降级逻辑。
- 正常偏差 → 静默更新基线（benchmark 接口已自动写回 DB）

## 外部调度触发的闲时校准场景

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
     否则所有服务器同时承受压力，违背安全约束。
  4. 每个节点完成后归档结果到 4-tests/batch-testing/outputs/benchmark/，与上次归档对比
  5. RTF 偏差 > 20% → 在报告中标注"⚠ 性能异常"并通知运维（当前后端不支持 API 标记 DEGRADED）
  6. 正常 → 静默更新基线（benchmark 接口已自动写回 DB）
```

## 与其他 Skill 的协作

| 协作场景 | 进入的 Skill 规程 | 时机 | 交接输入 | 交接输出 |
|---------|------------------|------|---------|---------|
| 入口编排需要确认服务器状态 | `funasr-task-manager-channel-intake` | 用户请求 benchmark 时从 intake 跳转 | `server_id`（可选） | benchmark 结果 / 推荐并发数 |
| 测试前确保环境干净 | `funasr-task-manager-reset-test-db` | benchmark 前需要清库时 | 脚本参数 | JSON 状态报告 |
| 验证端到端链路 | `funasr-task-manager-web-e2e` | benchmark 后验证调度是否正确 | profile 名称 | 测试报告 |

> 注意：本 Skill 不负责创建转写任务、交付结果、检查文件格式。这些职责分别属于 `channel-intake`、`result-delivery`、`media-preflight`。

## 错误处理规范

| 错误场景 | Agent 应做的事 | 不应做的事 |
|---------|--------------|----------|
| Admin token 缺失或 403 | 报告"需要 admin 权限才能执行 benchmark" | 尝试用普通 API Key 重试 |
| 服务器 OFFLINE | 先执行 probe，报告 probe 结果 | 直接发起 benchmark |
| Benchmark 超时（> 15 分钟） | 报告"benchmark 超时"，建议检查网络或服务器 | 无限等待 |
| NDJSON 解析失败 | 记录原始行，报告"收到无法解析的进度事件" | 静默忽略 |
| 退化检测触发 | 解释退化原因和建议 | 忽略退化继续测试更高并发 |
| 队列非空但用户坚持 | 用户确认后继续，但在报告中标注"非空闲条件下执行" | 不警告直接执行 |
| 网络错误 / 连接断开 | 报告"与服务器通信中断"，建议检查网络 | 自动重试多次 |

## 相关文件

- `3-dev/src/backend/app/api/servers.py`：benchmark 端点定义（`POST /{id}/benchmark` 和 `POST /benchmark`）
- `3-dev/src/backend/app/services/server_benchmark.py`：benchmark 核心逻辑
- `3-dev/src/backend/app/services/scheduler.py`：调度器如何使用 `rtf_baseline` / `max_concurrency`
- `3-dev/src/backend/cli/commands/server.py`：CLI benchmark 命令
- `references/project-context.md`：benchmark 相关 API/CLI 端点与权限
- `references/safety-checklist.md`：前置安全检查清单
- `references/ndjson-events.md`：NDJSON 事件参考
- `references/result-templates.md`：结果解读与汇报模板
