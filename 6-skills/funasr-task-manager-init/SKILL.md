---
name: funasr-task-manager-init
description: >
  Bootstrap the funasr-task-manager development environment from scratch.
  Use when: first-time setup, checking if repo is cloned, installing
  dependencies (Python/Unicorn or Docker), or starting backend/frontend
  services for the first time.
---

# 环境初始化与启动

`funasr-task-manager-init` 负责所有前期环境准备工作：检查仓库是否已克隆、判断后端/前端是否已启动、根据用户选择的环境方式完成安装并启动服务。

## 触发条件

### 自动触发

- Agent 首次接触本项目时
- 用户执行任何需要后端运行的操作（如转写、benchmark），但后端不可达时
- `GET /health` 返回连接失败或非 200 时

### 用户显式触发

- 用户说"初始化环境""安装项目""启动后端""setup""init"
- 用户说"换 Docker 部署""重新安装"

### 关键词

`初始化` / `安装` / `部署` / `启动` / `setup` / `init` / `bootstrap` / `docker` / `unicorn` / `环境`

## 执行流程

### Phase 1：检查仓库状态

1. 检查当前工作目录是否为 `funasr-task-manager` 仓库
   - 判断依据：存在 `3-dev/src/backend/app/main.py` 且 `.git/` 目录存在
   - 不存在 → 提示用户克隆：
     ```bash
     git clone https://github.com/user/funasr-task-manager.git
     cd funasr-task-manager
     ```
   - 存在 → 继续 Phase 2

### Phase 2：检查后端运行状态

1. 检查后端是否已启动（**核心判断条件，Agent 工作流仅依赖后端 API**）
   - 请求 `GET http://localhost:15797/health`
   - 返回 `{"status": "ok"}` → 后端已运行
   - 连接失败或非 200 → 后端未启动

2. 判断结果：
   - 后端已运行 → 报告"环境就绪"，**询问是否执行后续可选阶段**：
     > 环境已就绪。是否还需要：
     > 1. 安装/更新 Skills 到 Agent 平台（Phase 6）
     > 2. 配置渠道凭据（Phase 7）
     > 3. 注册后端为 systemd 服务（Phase 8，仅 Linux）
     > 4. 不需要，退出
   - 后端未启动 → 进入 Phase 3

> **注意**：前端（`http://localhost:15798`）是 Web UI，供人类操作员使用，Agent 工作流不依赖前端。不检查、不启动前端。

### Phase 3：选择安装方式

**向用户询问**（二选一）：

> 检测到服务未启动，请选择安装方式：
> 1. **Python 环境**（推荐开发）— 使用 Unicorn 管理 Python 3.13 环境
> 2. **Docker 环境**（推荐部署）— 使用 Docker Compose 一键启动

用户选择后进入对应分支。

### Phase 4A：Python 环境安装

按顺序执行以下步骤：

#### Step 1：检查现有 Python 环境是否已满足

先检查系统 Python 是否已经具备运行后端的全部依赖：

```bash
python3 -c "import fastapi; import sqlalchemy; import uvicorn; print('OK')" 2>/dev/null
```

- 输出 `OK` → **跳过 Step 2-3**，直接进入 Step 4（现有环境已可用）
- 失败 → 继续 Step 2

#### Step 2：尝试 Unicorn 环境（优先但非必选）

```bash
which unicorn && unicorn --version
```

- 可用 → 用 Unicorn 创建隔离环境：
  ```bash
  cd 3-dev/src/backend
  unicorn env create python3.13 --name funasr-backend
  unicorn env activate funasr-backend
  ```
- 不可用 → 回退到系统 Python + venv/pip，不要求安装 Unicorn

#### Step 3：安装后端依赖

```bash
cd 3-dev/src/backend
pip install -e ".[dev]"
```

如果 `pyproject.toml` 不存在 `.[dev]`，则回退到：
```bash
pip install -r requirements.txt
```

验证核心依赖：
```bash
python3 -c "import fastapi; import sqlalchemy; import uvicorn; print('OK')"
```

#### Step 4：检查 ffmpeg / ffprobe

VAD 分段并行转写（长音频自动切分）依赖 ffprobe 获取精确时长，ffmpeg 用于静音检测和物理切段。缺少时分段功能会静默 fallback 到整文件转写，性能大幅下降。

```bash
ffprobe -version
ffmpeg -version
```

- 两者均可用 → 继续
- 不可用 → 按平台安装：
  - **Linux（有 sudo）**：`sudo apt-get install -y ffmpeg`
  - **Linux（无 sudo / 容器内）**：下载静态二进制包：
    ```bash
    mkdir -p ~/.local/bin
    cd /tmp
    curl -LO https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
    tar xf ffmpeg-release-amd64-static.tar.xz
    cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe ~/.local/bin/
    chmod +x ~/.local/bin/ffmpeg ~/.local/bin/ffprobe
    rm -rf ffmpeg-*
    ```
    确保 `~/.local/bin` 在 PATH 中：`export PATH="$HOME/.local/bin:$PATH"`
  - **macOS**：`brew install ffmpeg`
  - **Windows**：下载 https://www.gyan.dev/ffmpeg/builds/ 解压后加入 PATH
- 安装后再次验证 `ffprobe -version`

#### Step 5：初始化数据库

```bash
cd 3-dev/src/backend
python -m alembic upgrade head
```

#### Step 6：启动后端

```bash
cd 3-dev/src/backend
uvicorn app.main:app --host 0.0.0.0 --port 15797 --reload
```

启动后等待 5 秒，验证 `GET http://localhost:15797/health` 返回 `{"status": "ok"}`。

**后端启动成功即可进入 Phase 5。** 前端（Vue Web UI）是可选的，Agent 工作流不依赖前端。

### Phase 4B：Docker 环境安装

#### Step 1：检查 Docker 是否可用

```bash
docker --version
docker compose version
```

- 两者均可用 → 继续
- Docker 不可用 → 引导安装：
  - Windows → 提示下载 Docker Desktop：https://www.docker.com/products/docker-desktop/
  - Linux → 提供安装命令：
    ```bash
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker $USER
    ```
  - 安装后要求用户重启终端，再次执行本 Skill

#### Step 2：检查 Docker 运行状态

```bash
docker info
```

- 正常输出 → 继续
- 报错"Cannot connect to the Docker daemon" → 提示启动 Docker Desktop 或 `sudo systemctl start docker`

#### Step 3：检查系统资源

- 可用内存 >= 4GB（推荐 8GB）
- 可用磁盘 >= 5GB
- 不满足 → 警告但不阻断

#### Step 4：构建并启动

```bash
docker compose up -d --build
```

等待容器启动，验证：
```bash
docker compose ps
# 应显示 backend 和 frontend 容器为 running/healthy
```

验证 `GET http://localhost:15797/health` 返回 `{"status": "ok"}`。

### Phase 5：启动验证与报告

所有步骤完成后，输出验证报告：

```
✅ funasr-task-manager 后端已就绪

  安装方式: {python/docker}
  后端地址: http://localhost:15797
  健康检查: ✅ 通过
  数据库:   ✅ 已迁移到最新版本
  ffprobe:  ✅ 可用（版本 x.x.x）

  下一步:
  - 注册 FunASR 服务器: python -m cli server register <ws://...>
  - 上传并转写: python -m cli transcribe <audio-file>
```

输出报告后，**必须询问用户是否执行 Phase 6/7/8**（不可跳过此询问）：

> 是否需要执行以下可选配置？
> 1. 安装/更新 ASR Skills 到 Agent 平台（推荐）
> 2. 配置飞书/企微/Slack 渠道凭据
> 3. 注册后端为 systemd 用户级服务（仅 Linux，无需 sudo，开机自启 + 崩溃重启）
> 4. 全部执行
> 5. 跳过

### Phase 6：安装 Skills 到 Agent 平台

**目的**：将 `6-skills/` 下的所有 Skill 安装到 Agent 平台的自动加载目录，使 Agent 启动时即具备 ASR 转写能力，不需要用户手动指挥"去 repo 里学一下 skill"。

**向用户询问**：

> 是否需要将 ASR Skills 安装到 Agent 平台？安装后 Agent 启动时会自动加载这些技能，可以自主识别文件并完成转写。
>
> 请问你使用的是哪个 Agent 平台？
> 1. **OpenClaw** — Skills 安装到 `~/.openclaw/workspace-{name}/skills/`
> 2. **Hermes** — Skills 安装到 `~/.hermes/skills/`
> 3. **Cursor** — Skills 安装到 `{project}/.cursor/skills/` 或 `~/.cursor/skills-cursor/`
> 4. **其他 / 不安装** — 跳过此步骤

用户选择后进入对应分支。

#### 6A：OpenClaw

先确定 workspace 名称（向用户询问或自动检测当前活跃 workspace）：

```bash
# 列出已有 workspace（选择其中一个）
ls ~/.openclaw/workspace-*/

# 向用户确认目标 workspace 名称，例如：asr、default、my-project 等
WORKSPACE_NAME="<用户确认的 workspace 名>"
WORKSPACE_SKILLS="$HOME/.openclaw/workspace-$WORKSPACE_NAME/skills"
REPO_SKILLS="{repo_root}/6-skills"

mkdir -p "$WORKSPACE_SKILLS"

for skill_dir in "$REPO_SKILLS"/funasr-task-manager-*/; do
  skill_name=$(basename "$skill_dir")
  rm -rf "$WORKSPACE_SKILLS/$skill_name"
  cp -r "$skill_dir" "$WORKSPACE_SKILLS/$skill_name"
  echo "✅ $skill_name"
done

# 安装 ASR 工作流文档到 workspace
WORKSPACE_ROOT="$HOME/.openclaw/workspace-$WORKSPACE_NAME"
cp "$REPO_SKILLS/_shared/ASR-WORKFLOW.md" "$WORKSPACE_ROOT/ASR-WORKFLOW.md"
echo "✅ ASR-WORKFLOW.md"

# 向 AGENTS.md 追加 ASR 段落（幂等：已有则先删除旧版再追加新版）
if grep -q "BEGIN:funasr-task-manager" "$WORKSPACE_ROOT/AGENTS.md" 2>/dev/null; then
  sed '/<!-- BEGIN:funasr-task-manager/,/<!-- END:funasr-task-manager/d' "$WORKSPACE_ROOT/AGENTS.md" > "$WORKSPACE_ROOT/AGENTS.md.tmp"
  mv "$WORKSPACE_ROOT/AGENTS.md.tmp" "$WORKSPACE_ROOT/AGENTS.md"
fi
cat "$REPO_SKILLS/_shared/AGENTS-asr-section.md" >> "$WORKSPACE_ROOT/AGENTS.md"
echo "✅ AGENTS.md 已追加 ASR 转写段落"
```

验证：

```bash
ls "$WORKSPACE_SKILLS"/funasr-task-manager-*/SKILL.md | wc -l
test -f "$WORKSPACE_ROOT/ASR-WORKFLOW.md" && echo "✅ ASR-WORKFLOW.md 已安装"
```

#### 6B：Hermes

```bash
HERMES_SKILLS="$HOME/.hermes/skills"
REPO_SKILLS="{repo_root}/6-skills"

mkdir -p "$HERMES_SKILLS"

for skill_dir in "$REPO_SKILLS"/funasr-task-manager-*/; do
  skill_name=$(basename "$skill_dir")
  rm -rf "$HERMES_SKILLS/$skill_name"
  cp -r "$skill_dir" "$HERMES_SKILLS/$skill_name"
  echo "✅ $skill_name"
done

# 安装 ASR 工作流文档 + 追加 AGENTS.md ASR 段落
HERMES_ROOT="$HOME/.hermes"
cp "$REPO_SKILLS/_shared/ASR-WORKFLOW.md" "$HERMES_ROOT/ASR-WORKFLOW.md"
echo "✅ ASR-WORKFLOW.md"

if grep -q "BEGIN:funasr-task-manager" "$HERMES_ROOT/AGENTS.md" 2>/dev/null; then
  sed '/<!-- BEGIN:funasr-task-manager/,/<!-- END:funasr-task-manager/d' "$HERMES_ROOT/AGENTS.md" > "$HERMES_ROOT/AGENTS.md.tmp"
  mv "$HERMES_ROOT/AGENTS.md.tmp" "$HERMES_ROOT/AGENTS.md"
fi
cat "$REPO_SKILLS/_shared/AGENTS-asr-section.md" >> "$HERMES_ROOT/AGENTS.md"
echo "✅ AGENTS.md 已追加 ASR 转写段落"
```

#### 6C：Cursor

Cursor 的 skill 目录分为用户级和项目级：

- **用户级**（所有项目共享）：`~/.cursor/skills-cursor/`
- **项目级**（仅当前项目）：`{project}/.cursor/skills/`

推荐安装到项目级目录：

**Linux / macOS**：

```bash
PROJECT_SKILLS="{repo_root}/.cursor/skills"
REPO_SKILLS="{repo_root}/6-skills"

mkdir -p "$PROJECT_SKILLS"

for skill_dir in "$REPO_SKILLS"/funasr-task-manager-*/; do
  skill_name=$(basename "$skill_dir")
  rm -rf "$PROJECT_SKILLS/$skill_name"
  ln -sfn "$(cd "$skill_dir" && pwd)" "$PROJECT_SKILLS/$skill_name"
  echo "✅ $skill_name"
done
```

**Windows PowerShell**：

```powershell
$ProjectSkills = "{repo_root}\.cursor\skills"
$RepoSkills = "{repo_root}\6-skills"

New-Item -ItemType Directory -Force -Path $ProjectSkills | Out-Null

Get-ChildItem -Directory "$RepoSkills\funasr-task-manager-*" | ForEach-Object {
    $target = Join-Path $ProjectSkills $_.Name
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    # 符号链接需要管理员权限，降级为目录拷贝
    Copy-Item -Recurse $_.FullName $target
    Write-Host "✅ $($_.Name)"
}
```

#### 6D：跳过

记录日志，提示用户可以稍后手动安装：

```
ℹ️ 已跳过 Skill 安装。如需后续安装，请将 6-skills/funasr-task-manager-*/ 目录
   复制或链接到你的 Agent 平台的 skills 加载目录。
```

#### 安装后验证

输出已安装的 7 个 Skill 清单 + ASR-WORKFLOW.md 状态，确认安装目录和数量。

### Phase 7：配置渠道凭据（可选）

如果 Agent 需要通过聊天渠道（飞书、企业微信、Slack 等）接收用户文件，必须预配置渠道 API 凭据。否则 Agent 在收到文件时会花数分钟探索鉴权路径。

**向用户询问**：

> Agent 是否需要从聊天渠道接收文件？
> 1. **飞书/Lark** — 需要 `app_id` + `app_secret`
> 2. **企业微信** — 需要 `corpid` + `corpsecret`
> 3. **Slack** — 需要 Bot OAuth Token
> 4. **不需要 / 仅 CLI** — 跳过

各渠道的详细配置步骤、环境变量写入方式和验证命令见 [references/channel-credentials.md](references/channel-credentials.md)。

### Phase 8：systemd 用户级服务守护（可选，仅 Python 环境 + Linux）

仅适用于 Python 环境安装的后端（无论 Unicorn venv 还是系统 Python）。Docker 安装应使用 `docker compose` 的 `restart: always`，不需要本流程。

> **⚠️ 必须使用用户级服务（`systemctl --user`），不要使用系统级服务（`/etc/systemd/system/`）**。
> 理由：后端以普通用户身份运行，所有文件在用户目录下，监听 15797 非特权端口，不需要 root 权限。使用用户级服务可以：
> - 无需 `sudo`，Agent 可直接管理服务生命周期
> - 与 OpenClaw、Hermes、Cursor 等同样运行在用户空间的 Agent 工具统一管理
> - 避免系统级权限纠缠（之前系统级服务导致需要 sudo 的问题就此消除）

**前置检查**：`uname -s` 返回 `Linux` 且 `which systemctl` 成功且后端非 Docker 运行。非 Linux → 告知"仅适用于 Linux"，macOS 建议 `nohup` 或 launchd。

**如果检测到旧的系统级服务**：

```bash
# 检查是否存在系统级服务
if systemctl list-unit-files funasr-task-manager-backend.service | grep -q funasr; then
  echo "⚠️ 检测到旧的系统级服务，需要先迁移"
  # 展示以下命令让用户确认执行：
  # sudo systemctl disable --now funasr-task-manager-backend
  # sudo rm /etc/systemd/system/funasr-task-manager-backend.service
  # sudo systemctl daemon-reload
fi
```

**交互流程**：

1. 逐个候选 Python 执行 `import uvicorn`，**仅验证通过的才可选定**（路径存在 ≠ 可用），全部失败则中止
2. 生成 service 文件内容，**展示完整内容后请求用户确认再写入**
3. service 文件写入位置：`~/.config/systemd/user/funasr-task-manager-backend.service`
4. unit 文件关键差异（与系统级对比）：
   - **不要**包含 `User=` 行（用户级服务自动以当前用户运行）
   - `WantedBy=default.target`（而非 `multi-user.target`）
5. 已有同名 service → 先展示 diff 让用户选择覆盖/跳过
6. `systemctl --user daemon-reload && systemctl --user enable --now funasr-task-manager-backend`
7. 等待 5 秒后验证 `systemctl --user is-active funasr-task-manager-backend` + `curl /health`
8. 确保 `loginctl enable-linger $USER`（使服务在用户未登录时也能运行）

**安全规则**：写入 `~/.config/systemd/user/` 不需要 `sudo`。唯一可能需要 `sudo` 的操作是清理旧的系统级服务，此时必须展示完整命令让用户确认。

详细的 unit 文件模板、参数收集流程、验证输出和故障排查见 [references/systemd-setup.md](references/systemd-setup.md)。

## 错误处理

| 场景 | Agent 应做的事 | 不应做的事 |
|------|--------------|----------|
| Unicorn 安装失败 | 回退到直接使用系统 Python，提示手动创建 venv | 放弃安装 |
| pip install 报错 | 展示完整错误日志，建议检查网络或 Python 版本 | 静默跳过 |
| ffprobe 安装失败 | 展示错误，提示手动安装方法；标记 warning 但不阻断后续步骤 | 跳过不报告（会导致分段功能静默失效） |
| Docker build 失败 | 展示 build 日志最后 30 行，分析原因 | 反复重试 |
| 端口被占用 | 提示杀掉占用进程或换端口 | 强制 kill |
| 数据库迁移失败 | 展示错误，建议 `alembic downgrade base && alembic upgrade head` | 删除数据库文件 |
| Skill 目录不存在 | 创建目录后重试 | 跳过 Skill 安装 |
| Agent 平台无法识别 | 提供通用说明，让用户手动复制到对应目录 | 猜测平台路径 |
| systemctl 不可用 | 提示替代方案（nohup、screen、tmux）或展示命令让用户手动执行 | 强行写入 |
| 已有系统级服务 | 引导用户先 `sudo systemctl disable --now` 旧服务再创建用户级服务 | 直接覆盖或同时运行两个实例 |

## 与其他 Skill 的关系

- **前置 Skill**：本 Skill 是所有其他 Skill 的前置条件——后端不可达时，其他 Skill 均无法运行
- **被 `funasr-task-manager-channel-intake` 引用**：intake 在 Phase 2 检查后端健康时，如果失败可引导用户进入本 Skill
- **被 `funasr-task-manager-server-benchmark` 引用**：benchmark 需要后端运行

## 相关文件

- `3-dev/src/backend/pyproject.toml` 或 `requirements.txt`：后端依赖
- `3-dev/src/frontend/package.json`：前端依赖
- `docker-compose.yml`：Docker 编排 / `3-dev/src/backend/alembic/`：数据库迁移