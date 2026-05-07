# 平台适配说明

> 本文件说明各智能体平台在执行 `funasr-task-manager-local-batch-transcribe` Skill 时的差异化实现方式。
> 核心流程（Phase 0-7）对所有平台一致，差异仅在命令执行方式和进度监控机制上。

## 通用能力要求

各平台须具备以下能力：

- 本地文件系统读写
- Shell 命令执行（bash 或 PowerShell）
- HTTP 请求（通过 curl、httpie 或平台原生方式）
- 向用户输出进度与状态反馈

## Claude Code / Cursor

### 命令执行

- 使用 Shell 工具，并将 `working_directory` 设为 `3-dev/src/backend`
- 长时命令可通过 `block_until_ms: 0` 后台执行
- 读取终端输出文件以获取进度更新

### 进度监控实现（≤50 个文件时优先 Layer 1）

```
1. 使用 CLI：`--batch --poll-interval 10`
2. 设置 block_until_ms: 0，将命令置于后台
3. 周期性读取终端输出文件检查进度
4. 解析 stdout 行，形如 "[Xs] ✓ filename → SUCCEEDED (N/M)"
5. 每约 30 秒或每完成 5 个文件向用户汇报一次
```

### 进度监控实现（>50 个文件时使用 Layer 2）

```
1. 分块提交时使用 `--no-wait --json-summary`
2. 循环执行：`python -m cli task list --group {gid} --json`，间隔 15–30 秒
3. 解析 JSON，跨多个 group 聚合统计
4. 向用户汇报并更新 manifest
5. 通过 Shell 的 sleep 或 Await 等待下一轮
```

### 特殊注意

- 路径分隔符依操作系统使用正斜杠或 Windows 反斜杠
- 在 `3-dev/src/backend` 目录下执行 `python -m cli`
- 终端输出可通过 `.cursor/projects/.../terminals/` 下的终端文件获取

## Codex

### 命令执行

- 使用带 bash 的沙箱环境
- 命令在隔离容器中运行
- 文件系统为仓库检出目录

### 进度监控实现

- Codex 在单次会话轮次内的循环能力有限
- 建议：Layer 2，拆成显式的顺序命令

```
1. 使用 `--no-wait --json-summary` 逐个 chunk 提交（每 chunk 一条命令）
2. 监控：执行单条 `python -m cli task wait --group {gid} --timeout 3600`
3. wait 结束后从输出读取最终状态
4. 若存在多个 group：依次对每个 group 执行 wait
```

### 特殊注意

- 不能写无限循环；须在会话超时内结束
- 每条命令执行后更新 manifest
- 仅 Linux 路径（无 Windows）
- 超大批量可能需要拆到多轮用户对话中完成

## OpenClaw

### 命令执行

- 长时任务使用 `background: true`
- 后台命令结束后须主动向用户报告状态
- 通过工作区目录访问文件系统

### `send_user_notice()` 实现（首选）

OpenClaw runtime 暴露 `message` tool，**必须优先使用它发送所有阶段通知**：

```json
{"name": "message", "arguments": {"action": "send", "message": "⏳ Phase 4：正在提交第 1/3 批..."}}
```

发送文件附件时附带 `filePath`：

```json
{"name": "message", "arguments": {"action": "send", "message": "✅ 结果已生成", "filePath": "/tmp/funasr-task-manager/results/file.txt"}}
```

成功判断：toolResult 中 `ok == true`。失败时记录但不阻塞主流程。

**禁止**把阶段通知仅写入普通 assistant 文本——普通文本会被 turn 缓冲，直到整个工具调用链结束后才推送到飞书。

### 异步调度模式（推荐）

OpenClaw 支持子 Agent 并发能力时，采用**主 Agent 调度 + 子 Agent 监控**模式：

```
1. 主 Agent: task-group scan → send_user_notice → task-group submit → send_user_notice
2. 主 Agent: 委托子 Agent 执行 batch-monitor（传递 task_group_ids）
3. 主 Agent: 释放，继续接新任务
4. 子 Agent: 循环 task-group status → send_user_notice（进度）
5. 子 Agent: 全部完成 → task-group download → send_user_notice（汇总）→ 退出
```

子 Agent 同样通过 `message` tool 发送通知，复用 OpenClaw 已配好的飞书 Channel。

### 进度监控 Fallback（无子 Agent 能力时）

```
1. send_user_notice("Phase 4：开始提交...")       ← 先通知
2. 提交分块（task-group submit）
3. 监控：task-group status {gid} --output json
4. send_user_notice("进度：35/50 已完成...")      ← 每轮通知
5. 重复直至全部进入终态
```

### 特殊注意

- **关键**：每次后台操作后必须通过 `send_user_notice()` 发送进度通知（调用 `message` tool）
- 2026-04-28 经验：OpenClaw 智能体容易「静默执行」——Skill 明确要求每个阶段都要通知用户
- 2026-05-05 排查：批量转写 session 中 Agent 有 `message` tool 但未调用，所有通知被 turn 缓冲后统一送达
- 2026-05-06 架构升级：采用子 Agent 监控播报模式，主 Agent 不再被长批量任务绑死
- 主目录使用 `$HOME`，不要写死用户名
- Skill 安装路径：`~/.openclaw/workspace-{name}/skills/`

## Hermes

### 命令执行

- 通过本地终端获得 Shell
- Linux/macOS 上支持 bash/zsh
- Skill 位于 `~/.hermes/skills/`

### `send_user_notice()` 实现

Hermes 若暴露 `message` tool（类似 OpenClaw），优先使用。否则 fallback 到 CLI：

```bash
python -m cli notify send --text "⏳ Phase 4：正在提交..."
```

### 进度监控实现

- 与 OpenClaw 类似，但可直接用终端

```
1. send_user_notice("Phase 4：开始扫描...")       ← 先通知
2. 小批量可用 Layer 1（CLI 阻塞模式）
3. 大批量：Layer 2 + curl 轮询
4. 若已安装 jq，可解析进度，例如：
   curl -s .../task-groups/{gid} | jq '.progress'
5. send_user_notice("进度更新...")                ← 每轮通知
```

### 特殊注意

- 路径变量：使用 `$HOME`，项目根通过 `git` 探测
- 须兼容 Linux 与 macOS 路径
- 不需要支持 Windows

## 跨平台路径处理

| 场景 | Linux/macOS | Windows (PowerShell) |
|------|-------------|---------------------|
| 项目根目录 | `$(git rev-parse --show-toplevel)` | `(git rev-parse --show-toplevel)` |
| CLI 工作目录 | `cd 3-dev/src/backend` | `cd 3-dev/src/backend` |
| manifest 路径 | `runtime/agent-local-batch/manifests/` | `runtime\agent-local-batch\manifests\` |
| 健康检查 | `curl -sf http://127.0.0.1:15797/health` | `Invoke-RestMethod http://127.0.0.1:15797/health` |

## 平台能力矩阵

| 能力 | Claude Code | Codex | OpenClaw | Hermes |
|------|------------|-------|----------|--------|
| 后台执行 | ✅（block_until_ms: 0） | ❌ | ✅（background: true） | ✅ |
| 子 Agent 并发 | ⚠️（受限） | ❌ | ✅（推荐） | ⚠️（待验证） |
| 文件系统写入 | ✅ | ✅ | ✅ | ✅ |
| Windows 支持 | ✅ | ❌ | ❌ | ❌ |
| `send_user_notice()` | CLI fallback | CLI fallback | **message tool**（首选） | message tool / CLI |
| 推荐监控模式 | Fallback（主 Agent 轮询） | Fallback | **异步（子 Agent 播报）** | 异步 / Fallback |

> **通知规范**：详见 `6-skills/_shared/CHANNEL-NOTIFICATION.md`。所有平台都必须通过 `send_user_notice()` 发送阶段通知，禁止依赖普通 assistant 文本作为实时通知手段。
