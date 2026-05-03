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

### 进度监控实现

```
1. 提交分块（上传/创建步骤可放后台）
2. 监控：直接发 HTTP GET
   curl -s http://127.0.0.1:15797/api/v1/task-groups/{gid} | python -m json.tool
3. 在 shell 中解析 JSON 响应
4. 每轮轮询后向用户汇报
5. 重复直至全部进入终态
```

### 特殊注意

- **关键**：每次后台操作后必须发送进度通知
- 2026-04-28 经验：OpenClaw 智能体容易「静默执行」——Skill 明确要求每个阶段都要通知用户
- 主目录使用 `$HOME`，不要写死用户名
- Skill 安装路径：`~/.openclaw/workspace-{name}/skills/`

## Hermes

### 命令执行

- 通过本地终端获得 Shell
- Linux/macOS 上支持 bash/zsh
- Skill 位于 `~/.hermes/skills/`

### 进度监控实现

- 与 OpenClaw 类似，但可直接用终端

```
1. 小批量可用 Layer 1（CLI 阻塞模式）
2. 大批量：Layer 2 + curl 轮询
3. 若已安装 jq，可解析进度，例如：
   curl -s .../task-groups/{gid} | jq '.progress'
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
| 循环轮询 | ✅（Await + 读终端） | ⚠️（有限） | ✅（需主动回报） | ✅ |
| 文件系统写入 | ✅ | ✅ | ✅ | ✅ |
| Windows 支持 | ✅ | ❌ | ❌ | ❌ |
| 推荐监控层 | 小批量 Layer 1 / 大批量 Layer 2 | Layer 2 | Layer 2 | Layer 1/2 |
