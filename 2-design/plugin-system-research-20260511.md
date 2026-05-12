# funasr-task-manager 插件系统调研报告

> 日期：2026-05-11
> 状态：调研完成，待讨论确认方案

## 1. 出发点与目标

### 核心问题

funasr-task-manager 的本质是 **ASR 任务的分配、调度与管理**。但围绕这个核心，用户有很多"附加需求"：

| 需求领域 | 举例 | 是否核心？ |
|----------|------|-----------|
| 云服务器管理 | 配置阿里云/腾讯云 GPU 按需拉起 FunASR 服务器 | ❌ 可选 |
| 校验与润色 | 转写后用 LLM 做校对/润色，配置模板和 API 地址 | ❌ 可选 |
| 质量评估 | CER 计算、标准文本比对、ASR 服务器效果检测 | ❌ 可选 |
| 渠道接入 | 飞书/企微/Slack 文件接收与通知 | ❌ 可选（已有但可插件化） |
| 格式导出 | SRT/VTT/Word 等多格式输出 | ❌ 可选 |
| 说话人分离 | Speaker diarization 后处理 | ❌ 可选 |

这些都是"锦上添花"但不应该塞进核心的功能。插件系统的目标就是 **让核心保持精简，附加功能按需加载**。

### 设计目标

1. **核心不膨胀**：core 只做任务调度，插件失败/缺失不影响核心流程
2. **用户按需安装**：`asr-cli plugin install cloud-server` 而不是编译期决定
3. **插件自带 Skill**：安装插件后对应的 Agent Skill 也可用，提升智能体自动化能力
4. **可验证**：每个插件有独立的健康检查和版本信息
5. **低门槛开发**：第三方开发者能快速写出一个插件

## 2. 业界调研

### 2.1 三种主流 Python 插件发现机制对比

| 机制 | 原理 | 优点 | 缺点 | 代表项目 |
|------|------|------|------|---------|
| **importlib + 目录扫描** | 扫描约定目录下的 Python 包 | 零依赖、直观、部署简单 | 无版本约束、无依赖管理 | Airflow plugins/ |
| **setuptools entry_points** | `pyproject.toml` 声明入口点，`importlib.metadata` 发现 | 标准库支持（3.8+）、pip 安装即注册 | 需要插件是独立 pip 包 | Flask extensions |
| **pluggy (hook 系统)** | 定义 hookspec → 插件实现 hookimpl → PluginManager 调度 | 多插件共存、执行顺序可控、成熟稳定 | 学习曲线稍高 | pytest, tox, conda |

### 2.2 参考项目分析

#### FastAPI Best Architecture（fba）

- 使用 `plugin.toml` 清单文件描述插件元数据
- 两种插件类型：**App-level**（独立路由）和 **Extend-level**（扩展已有路由）
- Redis 信号通知插件状态变更
- CLI 工具管理安装/卸载：`fba plugin install xxx`
- **评价**：完整但偏重，依赖 Redis，适合大型系统

#### Airflow Provider 系统

- 插件放在 `$AIRFLOW_HOME/plugins/` 目录
- 继承 `AirflowPlugin` 基类注册能力
- 支持 FastAPI apps、middleware、operator links 等扩展点
- 懒加载，进程重启才生效
- **评价**：简单可靠，但不支持运行时热加载

#### kanban-lite Capability Namespace

- 按 **能力命名空间** 组织：`card.storage`、`webhook.delivery`、`auth.identity`
- 5 层架构：配置归一化 → 提供者解析 → 能力包 → SDK 事件生命周期 → 宿主消费
- 支持同时挂载多个提供者到不同命名空间
- **评价**：解耦最彻底，适合需要"同一接口多种实现"的场景

#### pytask (pluggy)

- 使用 pluggy 的 hookspec/hookimpl 模式
- 通过命令行 `--hook-module` 或配置文件加载插件
- 也支持打包为独立 pip 包
- **评价**：轻量且灵活，非常适合我们的规模

### 2.3 关键结论

| 维度 | 我们的情况 | 推荐 |
|------|-----------|------|
| 项目规模 | 中小型，单体 FastAPI | 不需要 fba 那么重的方案 |
| 插件数量 | 初期 3-5 个，远期 ~10 个 | 不需要 entry_points 的分发能力 |
| 部署方式 | 单机 + systemd / Docker | 目录扫描最适合 |
| 核心需求 | 扩展 API + 注入处理流水线 + 自带 Skill | 需要 hook 系统 |
| 开发门槛 | 要低，不能要求用户打 pip 包 | 目录拷贝 > pip install |

## 3. 推荐方案：目录扫描 + 轻量 Hook 系统

### 3.1 总体架构

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Core                          │
│  tasks / servers / files / health / scheduler            │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │             Plugin Manager                        │   │
│  │  discover() → validate() → load() → register()   │   │
│  └──────┬───────────────────────────────────────────┘   │
│         │                                               │
│  ┌──────┴──────────────────────────────────────────┐    │
│  │              Hook Registry                       │    │
│  │  on_task_created / on_transcription_complete /   │    │
│  │  on_result_format / on_server_health_check / ... │    │
│  └──────┬──────────────────────────────────────────┘    │
│         │                                               │
├─────────┼───────────────────────────────────────────────┤
│  plugins/                                               │
│  ├── cloud-server/      → API + Hook + Skill            │
│  ├── text-polish/       → Hook + Skill                  │
│  ├── quality-eval/      → API + Hook + Skill            │
│  └── export-formats/    → Hook + Skill                  │
└─────────────────────────────────────────────────────────┘
```

### 3.2 插件目录结构约定

每个插件是 `plugins/` 下的一个目录，包含固定结构：

```
plugins/
└── cloud-server/                  # 插件目录名 = 插件 ID
    ├── plugin.toml                # 插件清单（必选）
    ├── __init__.py                # 插件入口（必选）
    ├── api.py                     # FastAPI 路由（可选）
    ├── hooks.py                   # Hook 实现（可选）
    ├── models.py                  # SQLAlchemy 模型（可选）
    ├── config.py                  # 插件配置 schema（可选）
    ├── requirements.txt           # 额外依赖（可选）
    ├── skill/                     # 配套 Agent Skill（可选）
    │   └── SKILL.md
    └── README.md                  # 插件说明（可选）
```

### 3.3 plugin.toml 清单格式

```toml
[plugin]
name = "cloud-server"
version = "0.1.0"
description = "云服务器生命周期管理：按需拉起/释放 FunASR GPU 实例"
author = "funasr-task-manager team"
min_core_version = "0.4.18"       # 依赖的核心最低版本
tags = ["cloud", "infra"]

[plugin.capabilities]
# 声明此插件提供哪些能力（用于依赖解析和 UI 展示）
provides = ["server.provision", "server.teardown"]
requires = []                      # 依赖其他插件的能力

[settings]
# 插件默认配置（可被 .env 或 API 覆盖）
CLOUD_PROVIDER = "aliyun"
CLOUD_REGION = "cn-beijing"
CLOUD_INSTANCE_TYPE = "ecs.gn6i-c4g1.xlarge"
```

### 3.4 插件入口接口

```python
# plugins/cloud-server/__init__.py
from app.plugins.base import PluginBase, PluginMeta

class Plugin(PluginBase):
    """每个插件必须在 __init__.py 中导出 Plugin 类"""

    meta = PluginMeta(
        name="cloud-server",
        version="0.1.0",
    )

    async def on_enable(self, app, config):
        """插件启用时调用：注册路由、连接资源等"""
        from .api import router
        app.include_router(router, prefix="/api/v1/plugins/cloud-server", tags=["plugin:cloud-server"])

    async def on_disable(self):
        """插件禁用时调用：清理资源"""
        pass

    def get_hooks(self):
        """返回此插件实现的 hook 函数映射"""
        from .hooks import hooks
        return hooks

    def health_check(self) -> dict:
        """插件健康检查，聚合到 /health 响应"""
        return {"status": "ok", "version": self.meta.version}
```

### 3.5 Hook 系统设计

不引入 pluggy 依赖，自建轻量 Hook 注册表，因为我们的 hook 数量有限且可控：

```python
# app/plugins/hooks.py

from typing import Callable, Any
from collections import defaultdict

class HookRegistry:
    """轻量级同步/异步 hook 注册表"""

    def __init__(self):
        self._hooks: dict[str, list[Callable]] = defaultdict(list)

    def register(self, hook_name: str, fn: Callable, priority: int = 100):
        self._hooks[hook_name].append((priority, fn))
        self._hooks[hook_name].sort(key=lambda x: x[0])

    async def emit(self, hook_name: str, **kwargs) -> list[Any]:
        results = []
        for _, fn in self._hooks.get(hook_name, []):
            if asyncio.iscoroutinefunction(fn):
                results.append(await fn(**kwargs))
            else:
                results.append(fn(**kwargs))
        return results

    async def emit_pipeline(self, hook_name: str, data: Any, **kwargs) -> Any:
        """管道式 hook：前一个的输出是后一个的输入"""
        for _, fn in self._hooks.get(hook_name, []):
            if asyncio.iscoroutinefunction(fn):
                data = await fn(data, **kwargs)
            else:
                data = fn(data, **kwargs)
        return data

hook_registry = HookRegistry()
```

**核心预定义 Hook 列表**（Phase 1）：

| Hook 名称 | 触发时机 | 参数 | 用途示例 |
|-----------|---------|------|---------|
| `on_task_created` | 任务创建后 | task_id, file_info | 质量评估插件：记录原始文件信息 |
| `on_transcription_complete` | 单任务转写完成 | task_id, raw_text | 润色插件：自动校验/润色 |
| `on_result_format` | 结果格式化前 | task_id, text, format | 导出插件：注入新格式支持 |
| `on_server_registered` | ASR 服务器注册后 | server_id, server_info | 云服务器插件：标记来源 |
| `on_server_health_changed` | 服务器健康状态变化 | server_id, old, new | 云服务器插件：自动替换 |
| `on_batch_complete` | 批次全部完成 | batch_id, stats | 质量评估：生成批次报告 |
| `on_app_startup` | 应用启动时 | app | 通用初始化 |
| `on_app_shutdown` | 应用关闭时 | — | 资源清理 |

### 3.6 Plugin Manager

```python
# app/plugins/manager.py

class PluginManager:
    """插件发现、加载、管理"""

    def __init__(self, plugins_dir: Path):
        self.plugins_dir = plugins_dir
        self.loaded: dict[str, PluginBase] = {}
        self.enabled: dict[str, PluginBase] = {}

    def discover(self) -> list[str]:
        """扫描 plugins/ 目录，返回发现的插件 ID 列表"""

    def validate(self, plugin_id: str) -> ValidationResult:
        """校验 plugin.toml 格式、min_core_version 兼容性、依赖满足"""

    async def load(self, plugin_id: str) -> PluginBase:
        """导入插件模块，实例化 Plugin 类"""

    async def enable(self, plugin_id: str, app: FastAPI, config: dict):
        """启用插件：调用 on_enable，注册 hooks"""

    async def disable(self, plugin_id: str):
        """禁用插件：调用 on_disable，移除 hooks"""

    def get_status(self) -> list[dict]:
        """返回所有插件状态，用于 /health 和管理 API"""
```

### 3.7 核心集成点

在 `main.py` 的 `create_app()` 和 `lifespan` 中加入插件生命周期：

```python
# main.py lifespan 中
async def lifespan(app):
    # ... 现有启动逻辑 ...

    # 插件系统初始化
    from app.plugins.manager import plugin_manager
    discovered = plugin_manager.discover()
    logger.info("plugins_discovered", count=len(discovered), plugins=discovered)

    for plugin_id in plugin_manager.get_enabled_list():
        try:
            await plugin_manager.load(plugin_id)
            await plugin_manager.enable(plugin_id, app, config={})
            logger.info("plugin_enabled", plugin=plugin_id)
        except Exception as e:
            logger.warning("plugin_enable_failed", plugin=plugin_id, error=str(e))

    yield

    # 关闭时禁用所有插件
    for plugin_id in list(plugin_manager.enabled):
        await plugin_manager.disable(plugin_id)
```

在 `create_app()` 中追加管理 API：

```python
# 插件管理路由
from app.api.plugins import router as plugins_router
app.include_router(plugins_router)

# GET  /api/v1/plugins           → 列出所有插件及状态
# POST /api/v1/plugins/{id}/enable  → 启用
# POST /api/v1/plugins/{id}/disable → 禁用
# GET  /api/v1/plugins/{id}/health  → 单插件健康检查
# GET  /api/v1/plugins/{id}/config  → 获取插件配置
# PUT  /api/v1/plugins/{id}/config  → 更新插件配置
```

### 3.8 CLI 集成

```bash
# 安装插件（从 git repo 或本地目录）
asr-cli plugin install ./plugins/cloud-server
asr-cli plugin install https://github.com/user/asr-plugin-quality-eval.git

# 管理
asr-cli plugin list                    # 列出已安装插件
asr-cli plugin enable cloud-server     # 启用
asr-cli plugin disable cloud-server    # 禁用
asr-cli plugin info cloud-server       # 查看详情
asr-cli plugin config cloud-server     # 查看/修改配置
```

### 3.9 Skill 联动

插件的 `skill/` 目录在插件启用时自动链接到 Agent Skill 加载目录：

```
plugins/cloud-server/skill/SKILL.md
  → 安装时 symlink 到 6-skills/funasr-task-manager-plugin-cloud-server/SKILL.md
  → 或 init skill Phase 6 时一并同步
```

Skill 内可通过 CLI 或 API 调用插件能力：

```bash
# Skill 中示例
asr-cli plugin exec cloud-server provision --region cn-beijing
curl -X POST http://localhost:15797/api/v1/plugins/cloud-server/provision
```

## 4. 初期插件规划

### Phase 1：插件框架 + 2 个示范插件

| 插件 ID | 功能 | Hook 使用 | 有 API？ | 有 Skill？ |
|---------|------|----------|---------|-----------|
| `text-polish` | LLM 校验/润色转写结果 | `on_transcription_complete` (pipeline) | ✅ 配置 API 地址/模板 | ✅ 润色工作流 |
| `quality-eval` | CER 计算 + 标准文本比对 | `on_transcription_complete`, `on_batch_complete` | ✅ 上传标准文本、查看报告 | ✅ 评估工作流 |

### Phase 2：扩展插件

| 插件 ID | 功能 |
|---------|------|
| `cloud-server` | 阿里云/腾讯云 GPU 实例按需管理 |
| `export-formats` | SRT/VTT/Word/PDF 多格式导出 |
| `speaker-diarization` | 说话人分离后处理 |
| `webhook-notify` | 通用 Webhook 通知（Slack/飞书/企微统一） |

## 5. 方案对比：为什么不选其他方案

### 方案 A：pluggy（❌ 不推荐）

- 优点：pytest 验证的成熟方案，多 hook 执行顺序精确可控
- **不选原因**：我们 hook 数量少（<15 个），pluggy 的 hookspec 装饰器、marker 机制是过度设计；增加依赖和学习门槛

### 方案 B：entry_points（❌ 不推荐）

- 优点：pip install 即注册，标准生态
- **不选原因**：要求每个插件都是独立 pip 包，开发门槛太高；我们的用户场景是"下载目录 → 放进去 → 启用"

### 方案 C：FastAPI sub-application（❌ 不推荐）

- 优点：原生支持，每个插件是独立 FastAPI app mount
- **不选原因**：只解决路由扩展，不解决 hook 注入到核心流水线的需求（如转写后自动润色）

### 方案 D：目录扫描 + 轻量 Hook（✅ 推荐）

- 优点：零额外依赖、目录拷贝即安装、hook 满足流水线扩展需求、插件自带 Skill
- 缺点：需要自建 ~200 行的插件框架代码
- **选择原因**：最适合我们的项目规模、用户群体、部署方式

## 6. 风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 插件代码质量不可控 | 插件在独立命名空间加载，异常被 catch 不影响核心；health 端点暴露插件状态 |
| 插件间依赖冲突 | plugin.toml 声明 `requires` 能力；PluginManager 加载时拓扑排序 |
| 插件修改核心数据 | Hook 参数传递 copy/readonly 视图；pipeline hook 返回新数据而非修改原始对象 |
| 版本兼容性 | `min_core_version` 校验；Hook 签名变更时 deprecation 周期 |
| 数据库 schema 扩展 | 插件使用独立表（前缀 `plugin_xxx_`），不修改核心表；可选 alembic 分支 |

## 7. 实施路径建议

```
Phase 0 (v0.5.0)：核心框架
├── app/plugins/base.py        # PluginBase 抽象类
├── app/plugins/hooks.py       # HookRegistry
├── app/plugins/manager.py     # PluginManager
├── app/plugins/config.py      # 插件配置加载
├── app/api/plugins.py         # 管理 REST API
├── cli/commands/plugin.py     # CLI 命令
└── main.py 集成               # lifespan + create_app

Phase 1 (v0.5.1)：示范插件
├── plugins/text-polish/       # LLM 润色
└── plugins/quality-eval/      # CER 质量评估

Phase 2 (v0.6.0)：扩展
├── plugins/cloud-server/
├── plugins/export-formats/
└── 插件市场文档 + 开发者指南
```

## 8. 待讨论项

1. **插件存储位置**：放在 repo 内的 `plugins/` vs 外部目录 `~/.asr-plugins/`？
   - 建议：内置示范插件放 repo，用户自定义插件支持外部目录扫描（配置 `ASR_PLUGINS_DIR`）

2. **热加载 vs 重启生效**：是否需要不重启后端就能启用/禁用插件？
   - 建议：Phase 0 先重启生效（简单可靠），Phase 2 再考虑热加载

3. **插件数据库迁移**：插件需要建表时如何处理？
   - 建议：插件自带 `migrations/` 目录，PluginManager 启用时自动执行

4. **前端 UI 对接**：Web UI 是否需要展示插件管理界面？
   - 建议：Phase 0 CLI + API 即可，前端 Phase 2 再做
