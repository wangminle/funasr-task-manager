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

### Phase 2：检查服务运行状态

1. 检查后端是否已启动
   - 请求 `GET http://localhost:8000/health`
   - 返回 `{"status": "ok"}` → 后端已运行
   - 连接失败或非 200 → 后端未启动

2. 检查前端是否已启动
   - 请求 `GET http://localhost:5173/`
   - 返回 200 → 前端已运行
   - 连接失败 → 前端未启动

3. 判断结果：
   - 后端 + 前端均已运行 → 报告"环境就绪"，退出
   - 有服务未启动 → 进入 Phase 3

### Phase 3：选择安装方式

**向用户询问**（二选一）：

> 检测到服务未启动，请选择安装方式：
> 1. **Python 环境**（推荐开发）— 使用 Unicorn 管理 Python 3.13 环境
> 2. **Docker 环境**（推荐部署）— 使用 Docker Compose 一键启动

用户选择后进入对应分支。

### Phase 4A：Python 环境安装

按顺序执行以下步骤：

#### Step 1：检查 Unicorn 是否可用

```bash
unicorn --version
```

- 可用 → 继续
- 不可用 → 提示安装：
  ```bash
  pip install unicorn-env
  ```
  安装后再次验证

#### Step 2：创建 Python 3.13 环境

```bash
cd 3-dev/src/backend
unicorn env create python3.13 --name funasr-backend
unicorn env activate funasr-backend
```

验证 Python 版本：
```bash
python --version
# 应输出 Python 3.13.x
```

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
python -c "import fastapi; import sqlalchemy; import uvicorn; print('OK')"
```

#### Step 4：初始化数据库

```bash
cd 3-dev/src/backend
python -m alembic upgrade head
```

#### Step 5：安装前端依赖

```bash
cd 3-dev/src/frontend
npm install
```

#### Step 6：启动后端

```bash
cd 3-dev/src/backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

启动后等待 5 秒，验证 `GET http://localhost:8000/health` 返回 `{"status": "ok"}`。

#### Step 7：启动前端

```bash
cd 3-dev/src/frontend
npm run dev
```

启动后等待 5 秒，验证 `GET http://localhost:5173/` 返回 200。

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

验证 `GET http://localhost:8000/health` 返回 `{"status": "ok"}`。

### Phase 5：启动验证与报告

所有步骤完成后，输出验证报告：

```
✅ funasr-task-manager 环境已就绪

  安装方式: {python/docker}
  后端地址: http://localhost:8000
  前端地址: http://localhost:5173
  健康检查: ✅ 通过
  数据库:   ✅ 已迁移到最新版本

  下一步:
  - 注册 FunASR 服务器: python -m cli server register <ws://...>
  - 上传并转写: python -m cli transcribe <audio-file>
  - 打开前端: http://localhost:5173
```

## 错误处理

| 场景 | Agent 应做的事 | 不应做的事 |
|------|--------------|----------|
| Unicorn 安装失败 | 回退到直接使用系统 Python，提示手动创建 venv | 放弃安装 |
| pip install 报错 | 展示完整错误日志，建议检查网络或 Python 版本 | 静默跳过 |
| Docker build 失败 | 展示 build 日志最后 30 行，分析原因 | 反复重试 |
| 端口被占用 | 提示杀掉占用进程或换端口 | 强制 kill |
| 数据库迁移失败 | 展示错误，建议 `alembic downgrade base && alembic upgrade head` | 删除数据库文件 |

## 与其他 Skill 的关系

- **前置 Skill**：本 Skill 是所有其他 Skill 的前置条件——后端不可达时，其他 Skill 均无法运行
- **被 `funasr-task-manager-channel-intake` 引用**：intake 在 Phase 2 检查后端健康时，如果失败可引导用户进入本 Skill
- **被 `funasr-task-manager-server-benchmark` 引用**：benchmark 需要后端运行

## 相关文件

- `3-dev/src/backend/pyproject.toml` 或 `requirements.txt`：后端依赖
- `3-dev/src/frontend/package.json`：前端依赖
- `docker-compose.yml`：Docker 编排配置（如存在）
- `3-dev/src/backend/alembic/`：数据库迁移脚本
