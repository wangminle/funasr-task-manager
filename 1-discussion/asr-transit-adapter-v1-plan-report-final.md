# ASR 中转适配层方案 C - 汇总整合报告

> **课题**：多用户多ASR服务器离线转写中转适配层  
> **方案代号**：C / GLM-5（汇总整合）  
> **产出时间**：2026-02-26  
> **执行模型**：GLM-5  
> **输入**：方案 A（Kimi K2.5）+ 方案 B（Qwen3.5-plus）

---

## 1. 方案 A/B 核心差异对比

### 1.1 技术选型差异

| 维度 | 方案 A（Kimi） | 方案 B（Qwen3.5-plus） | 差异分析 |
|------|---------------|----------------------|----------|
| **任务队列** | Redis Streams / RabbitMQ | SQLite + 文件锁 | A 更适合高并发；B 更轻量 |
| **数据存储** | PostgreSQL + SQLAlchemy 2.0 | SQLite + 文件系统 | A 强事务、可扩展；B 零依赖、快速部署 |
| **可观测性** | Prometheus + Grafana | 结构化日志 + 内置指标端点 | A 专业监控；B 自包含 |
| **部署模式** | Docker Compose / 微服务 | 单体应用 + 多进程 | A 适合生产环境；B 适合快速验证 |
| **协议适配** | 抽象基类 + 具体适配器实现 | 策略模式 + YAML 配置驱动 | A 代码扩展；B 配置灵活 |

### 1.2 成本与规模差异

| 维度 | 方案 A | 方案 B | 差异分析 |
|------|--------|--------|----------|
| **开发成本** | 26 人天（4-5 周） | 6.5 人天（MVP） | B 节省 75% 开发时间 |
| **部署时间** | ~30 分钟（含中间件） | <30 分钟（单进程） | B 更快启动 |
| **适用规模** | 10-50 并发，30+ jobs/min | <1000 jobs/天 | A 吞吐更高 |
| **运维复杂度** | 中（需维护 Redis/PG） | 低（无外部依赖） | B 运维成本低 |

### 1.3 设计哲学差异

| 维度 | 方案 A | 方案 B |
|------|--------|--------|
| **定位** | 标准化、工业级、可扩展 | 轻量级、零依赖、快速上线 |
| **适用场景** | 中型团队、长期运营 | 小型团队、PoC 验证 |
| **扩展策略** | 水平扩展、微服务化 | 渐进演进、平滑迁移 |
| **风险承受** | 依赖中间件稳定性 | SQLite 并发写限制 |

---

## 2. 取舍决策与理由

### 2.1 整合策略：**渐进式演进路线**

**核心决策：采用方案 B 为起点，方案 A 为演进目标**

| 阶段 | 方案选择 | 理由 |
|------|----------|------|
| **M1-M2（PoC + 多节点联调）** | 方案 B | 快速验证核心链路，6.5 人天完成 |
| **M3（压测验证）** | 方案 B + 优化 | 确认 SQLite 瓶颈，决定是否迁移 |
| **M4（灰度上线）** | 方案 B → A 演进 | 如达到切换阈值，启动迁移 |

### 2.2 关键取舍理由

| 决策点 | 取舍选择 | 理由 |
|--------|----------|------|
| **初始队列选型** | SQLite + 文件锁 | 1) 日任务量预估 <1000；2) 快速上线验证；3) 无需维护 Redis |
| **数据库选型** | SQLite → PostgreSQL 预留迁移 | 1) MVP 用 SQLite 足够；2) Schema 设计兼容 PG；3) 迁移成本低 |
| **可观测性** | 结构化日志 + 内置指标 | 1) 不依赖外部监控；2) /metrics 端点兼容 Prometheus；3) 渐进接入 Grafana |
| **协议适配** | 策略模式 + YAML 配置 | 1) 新增协议无需改代码；2) 降低开发门槛；3) 易于测试 |
| **调度策略** | 动态槽位 + 加权公平（B 方案） | 1) 比 FIFO 更高效；2) 实现复杂度可控；3) 支持后续优先级扩展 |

### 2.3 不采纳的设计

| 设计项 | 来源 | 不采纳理由 |
|--------|------|------------|
| Celery 任务队列 | 方案 A | 初始规模无需 Celery 复杂度；SQLite 队列足够 |
| 固定并发 FIFO | 方案 A 原始 | 动态槽位更高效，性能提升 15-20% |
| 独立 Prometheus 部署 | 方案 A | 先用内置指标，达到阈值后再接入外部监控 |
| 完全微服务化 | 方案 A | 初期单体足够；渐进拆分更稳妥 |

---

## 3. 统一执行计划（Python-First）

### 3.1 技术栈确定

| 层级 | 技术选型 | 版本 | 说明 |
|------|----------|------|------|
| **Web 框架** | FastAPI | 0.109+ | 异步高性能，自动文档 |
| **ASGI 服务器** | Uvicorn | 0.27+ | 生产级 ASGI |
| **任务队列** | SQLite + asyncio | 3.35+ | WAL 模式，异步查询 |
| **数据存储** | SQLite | 3.35+ | 预留 PostgreSQL 迁移接口 |
| **ORM** | SQLAlchemy | 2.0+ | 异步支持，兼容 PG |
| **HTTP 客户端** | aiohttp | 3.8+ | 异步调用 ASR 节点 |
| **元信息解析** | ffprobe（ffmpeg） | 4.0+ | 外部进程调用 |
| **日志** | structlog | 23.0+ | 结构化 JSON 日志 |
| **配置管理** | Pydantic Settings | 2.0+ | 类型安全配置 |

### 3.2 核心模块设计（整合 A/B 优点）

```
asr-transit-adapter/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 入口
│   ├── config.py                  # 配置管理（Pydantic Settings）
│   ├── models/                    # 数据模型
│   │   ├── __init__.py
│   │   ├── job.py                 # Job 模型
│   │   ├── file.py                # File 模型
│   │   ├── node.py                # Node 模型
│   │   └── dispatch.py            # DispatchRecord 模型
│   ├── api/                       # API 路由
│   │   ├── __init__.py
│   │   ├── jobs.py                # 任务相关 API
│   │   ├── nodes.py               # 节点管理 API
│   │   └── metrics.py             # 指标暴露 API
│   ├── services/                  # 业务逻辑
│   │   ├── __init__.py
│   │   ├── upload.py              # 文件上传服务
│   │   ├── metadata.py            # 元信息提取服务
│   │   ├── scheduler.py           # 调度器（核心）
│   │   ├── dispatcher.py          # 任务分发
│   │   └── cleanup.py             # 清理服务
│   ├── adapters/                  # 协议适配层
│   │   ├── __init__.py
│   │   ├── base.py                # 抽象基类
│   │   ├── legacy.py              # 旧协议适配器
│   │   ├── new.py                 # 新协议适配器
│   │   └── registry.py            # 适配器注册（YAML 配置驱动）
│   ├── storage/                   # 存储层
│   │   ├── __init__.py
│   │   ├── database.py            # SQLite 连接管理
│   │   ├── repository.py          # 数据访问层
│   │   └── file_manager.py        # 文件管理
│   └── observability/             # 可观测性
│       ├── __init__.py
│       ├── logging.py             # 结构化日志配置
│       ├── metrics.py             # 指标定义
│       └── alerts.py              # 告警逻辑
├── config/
│   ├── adapters.yaml              # 协议适配器配置
│   ├── nodes.yaml                 # ASR 节点配置
│   └── settings.yaml              # 应用配置
├── tests/
│   ├── unit/
│   └── integration/
├── scripts/
│   ├── migrate_to_pg.py           # PostgreSQL 迁移脚本（预留）
│   └── benchmark.py                # 性能压测脚本
├── pyproject.toml
├── Dockerfile
└── README.md
```

### 3.3 数据库设计（SQLite 兼容 PostgreSQL）

```sql
-- jobs 表（任务核心）
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,           -- UUID v4
    user_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'dispatching', 'running', 'success', 'failed', 'cancelled')),
    priority INTEGER DEFAULT 0,        -- 预留优先级字段
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    node_id TEXT,                      -- 分配的 ASR 节点
    external_job_id TEXT,              -- ASR 系统返回的任务 ID
    result_url TEXT,
    error_message TEXT,
    eta_seconds INTEGER,               -- 预估剩余时间
    created_at INTEGER NOT NULL,       -- Unix timestamp (ms)
    started_at INTEGER,
    finished_at INTEGER,
    updated_at INTEGER
);

CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_user_id ON jobs(user_id);
CREATE INDEX idx_jobs_created_at ON jobs(created_at);

-- files 表（文件元信息）
CREATE TABLE files (
    file_id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_ms INTEGER,
    codec TEXT,
    sample_rate INTEGER,
    channels INTEGER,
    media_type TEXT CHECK(media_type IN ('audio', 'video')),
    checksum TEXT,
    created_at INTEGER NOT NULL
);

-- nodes 表（ASR 节点）
CREATE TABLE nodes (
    node_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    protocol_type TEXT NOT NULL,       -- 'legacy' / 'new'
    max_concurrency INTEGER NOT NULL DEFAULT 4,
    current_load INTEGER DEFAULT 0,
    health_status TEXT DEFAULT 'unknown' CHECK(health_status IN ('unknown', 'healthy', 'unhealthy', 'degraded')),
    last_health_check INTEGER,
    consecutive_failures INTEGER DEFAULT 0,
    avg_latency_ms INTEGER,            -- 平均响应延迟
    registered_at INTEGER NOT NULL,
    metadata TEXT                      -- JSON 额外信息
);

-- dispatch_log 表（调度审计日志）
CREATE TABLE dispatch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    dispatched_at INTEGER NOT NULL,
    completed_at INTEGER,
    result_code INTEGER,                -- HTTP 状态码
    latency_ms INTEGER,
    error_detail TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (node_id) REFERENCES nodes(node_id)
);

CREATE INDEX idx_dispatch_log_job_id ON dispatch_log(job_id);
CREATE INDEX idx_dispatch_log_node_id ON dispatch_log(node_id);
CREATE INDEX idx_dispatch_log_dispatched_at ON dispatch_log(dispatched_at);
```

### 3.4 调度器核心设计

```python
# services/scheduler.py
import asyncio
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()

@dataclass
class SchedulingResult:
    success: bool
    node_id: Optional[str] = None
    reason: str = ""

class WeightedFairScheduler:
    """加权公平队列调度器"""
    
    def __init__(self, config: dict):
        self.poll_interval = config.get('poll_interval', 5)  # 秒
        self.max_retry = config.get('max_retry', 3)
        self.node_health_threshold = config.get('node_health_threshold', 3)
        self._running = False
    
    async def start(self):
        """启动调度器"""
        self._running = True
        logger.info("scheduler_started", poll_interval=self.poll_interval)
        while self._running:
            try:
                await self._schedule_batch()
            except Exception as e:
                logger.error("scheduler_error", error=str(e))
            await asyncio.sleep(self.poll_interval)
    
    async def stop(self):
        """停止调度器"""
        self._running = False
        logger.info("scheduler_stopped")
    
    async def _schedule_batch(self):
        """批量调度"""
        # 1. 查询 queued 状态任务（按 created_at 排序）
        jobs = await self._fetch_queued_jobs()
        if not jobs:
            return
        
        # 2. 查询可用节点（healthy + 有空槽位）
        nodes = await self._fetch_available_nodes()
        if not nodes:
            logger.warning("no_available_nodes", queue_length=len(jobs))
            return
        
        # 3. 按加权公平策略分配
        for job in jobs:
            result = await self._dispatch_job(job, nodes)
            if result.success:
                nodes = await self._update_node_load(nodes, result.node_id, +1)
            else:
                logger.warning("dispatch_failed", job_id=job.job_id, reason=result.reason)
    
    async def _dispatch_job(self, job, nodes: List) -> SchedulingResult:
        """分发单个任务"""
        # 选择节点（加权公平）
        node = self._select_node(nodes)
        if not node:
            return SchedulingResult(success=False, reason="no_available_node")
        
        try:
            # 调用协议适配器
            adapter = self._get_adapter(node.protocol_type)
            external_job_id = await adapter.submit(job.file_path, job.asr_profile)
            
            # 更新任务状态
            await self._update_job_status(
                job.job_id, 
                status='running',
                node_id=node.node_id,
                external_job_id=external_job_id
            )
            
            # 记录调度日志
            await self._log_dispatch(job.job_id, node.node_id)
            
            return SchedulingResult(success=True, node_id=node.node_id)
            
        except Exception as e:
            logger.error("dispatch_exception", job_id=job.job_id, error=str(e))
            return SchedulingResult(success=False, reason=str(e))
    
    def _select_node(self, nodes: List) -> Optional[dict]:
        """加权公平节点选择"""
        import random
        
        available = [n for n in nodes if n['available_slots'] > 0]
        if not available:
            return None
        
        # 按可用槽位加权随机
        weights = [n['available_slots'] for n in available]
        return random.choices(available, weights=weights)[0]
```

### 3.5 协议适配器设计（YAML 配置驱动）

```yaml
# config/adapters.yaml
adapters:
  legacy_v1:
    class: "adapters.legacy.LegacyAdapter"
    endpoint_format: "http://{host}:{port}/api/v1/recognize"
    request_mapping:
      audio_file: "file"
      language: "params.lang"
      model: "params.model"
    response_mapping:
      text: "result.transcript"
      confidence: "result.confidence"
      duration_ms: "result.duration"
    error_mapping:
      400: "INVALID_PARAMS"
      500: "ASR_ERROR"
      timeout: "TIMEOUT"
    timeout_seconds: 300
    retry:
      max_attempts: 3
      backoff_seconds: 2

  new_v2:
    class: "adapters.new.NewAdapter"
    endpoint_format: "http://{host}:{port}/api/v2/transcribe"
    request_mapping:
      audio_file: "media"
      language: "config.language"
      model: "config.model_name"
    response_mapping:
      text: "data.text"
      confidence: "data.score"
      duration_ms: "data.processing_time_ms"
    error_mapping:
      400: "BAD_REQUEST"
      404: "NOT_FOUND"
      500: "INTERNAL_ERROR"
    timeout_seconds: 600
    retry:
      max_attempts: 2
      backoff_seconds: 5
```

```python
# adapters/base.py
from abc import ABC, abstractmethod
from typing import Dict, Any
from dataclasses import dataclass

@dataclass
class ASRResult:
    success: bool
    text: str = ""
    confidence: float = 0.0
    duration_ms: int = 0
    error_code: str = ""
    error_message: str = ""

class ASRAdapter(ABC):
    """ASR 协议适配器基类"""
    
    @abstractmethod
    async def submit(self, file_path: str, params: Dict[str, Any]) -> str:
        """提交任务，返回外部任务 ID"""
        pass
    
    @abstractmethod
    async def query(self, external_job_id: str) -> ASRResult:
        """查询任务状态与结果"""
        pass
    
    @abstractmethod
    async def cancel(self, external_job_id: str) -> bool:
        """取消任务"""
        pass
```

### 3.6 可观测性设计

```python
# observability/logging.py
import structlog
from datetime import datetime

def configure_logging():
    """配置结构化日志"""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

# 使用示例
logger = structlog.get_logger()
logger.info(
    "job_dispatched",
    job_id="550e8400-e29b-41d4-a716-446655440000",
    node_id="asr-node-01",
    latency_ms=150,
    user_id="user-123"
)
```

```python
# observability/metrics.py
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from fastapi import Response

# 指标定义
jobs_submitted = Counter('jobs_submitted_total', 'Total jobs submitted')
jobs_completed = Counter('jobs_completed_total', 'Total jobs completed', ['status'])
jobs_failed = Counter('jobs_failed_total', 'Total jobs failed', ['error_code'])

job_queue_length = Gauge('job_queue_length', 'Current queue length')
job_wait_duration = Histogram('job_wait_duration_seconds', 'Job wait time in queue')
job_process_duration = Histogram('job_process_duration_seconds', 'Job processing time')

node_available_slots = Gauge('node_available_slots', 'Available slots per node', ['node_id'])
node_health_status = Gauge('node_health_status', 'Node health status (1=healthy, 0=unhealthy)', ['node_id'])

async def metrics_endpoint():
    """Prometheus 指标端点"""
    return Response(
        content=generate_latest(),
        media_type="text/plain"
    )
```

### 3.7 文件管理设计

```
/data/asr-transit/
├── uploads/                    # 原始上传文件
│   └── 2026-02-26/            # 按日期分目录
│       ├── {job_id}_audio.mp4
│       └── {job_id}_voice.wav
├── processing/                # 处理中文件（符号链接）
│   └── {job_id} -> ../uploads/2026-02-26/{job_id}_audio.mp4
├── completed/                  # 完成文件（保留 7 天）
│   └── 2026-02-26/
│       └── {job_id}.json     # 转写结果
├── failed/                     # 失败文件（保留 30 天）
│   └── 2026-02-26/
│       └── {job_id}.json     # 错误详情
└── logs/
    └── 2026-02-26/
        └── app.log            # 应用日志
```

---

## 4. 决策矩阵

### 4.1 五维度评分（1-5 分，5 分最优）

| 维度 | 方案 A 评分 | 方案 B 评分 | 整合方案评分 | 取舍理由 |
|------|-------------|-------------|--------------|----------|
| **可行性** | 4 | 5 | **5** | B 方案技术栈简单，部署依赖少，可行性最高；整合方案继承 B 的轻量特性，同时预留 A 的扩展能力 |
| **复杂度** | 3 | 5 | **4** | A 需维护 Redis/PG，复杂度高；B 单进程简单；整合方案略增复杂度（配置驱动），但仍可控 |
| **成本** | 2 | 5 | **4** | A 开发 26 人天，运维需中间件；B 开发 6.5 人天，零中间件；整合方案渐进演进，初期成本低 |
| **扩展性** | 5 | 2 | **4** | A 天生支持水平扩展；B 扩展受限；整合方案通过预留迁移接口，支持渐进扩展 |
| **风险** | 3 | 3 | **4** | A 依赖中间件稳定性；B 有 SQLite 并发写风险；整合方案通过 WAL 模式 + 迁移预案缓解 |

### 4.2 详细评分说明

#### 可行性（Feasibility）

| 方案 | 评分 | 说明 |
|------|------|------|
| A | 4 | 依赖 Redis/PostgreSQL，需中间件运维能力；团队熟悉则可行 |
| B | 5 | 零外部依赖，Python 环境即可运行；部署门槛最低 |
| 整合 | 5 | 初期用 B 快速验证，后续渐进到 A；路径清晰，风险可控 |

#### 复杂度（Complexity）

| 方案 | 评分 | 说明 |
|------|------|------|
| A | 3 | 微服务架构、Celery 队列、Prometheus 监控，学习曲线陡 |
| B | 5 | 单体应用、SQLite 存储、内置日志，最简单 |
| 整合 | 4 | 比 B 略复杂（配置驱动、适配器模式），但比 A 简单很多 |

#### 成本（Cost）

| 方案 | 评分 | 开发成本 | 运维成本 | 扩展成本 |
|------|------|----------|----------|----------|
| A | 2 | 26 人天 | 中（需维护中间件） | 低（水平扩展容易） |
| B | 5 | 6.5 人天 | 低（无中间件） | 高（需重构迁移） |
| 整合 | 4 | 初期 6.5 人天 + 后期渐进投入 | 初期低，渐进增加 | 低（已预留迁移接口） |

#### 扩展性（Scalability）

| 方案 | 评分 | 水平扩展 | 功能扩展 | 协议扩展 |
|------|------|----------|----------|----------|
| A | 5 | 支持（微服务） | 支持（模块化） | 支持（适配器模式） |
| B | 2 | 受限（SQLite 写锁） | 受限（单体） | 支持（YAML 配置） |
| 整合 | 4 | 渐进支持 | 支持（配置驱动） | 支持（YAML + 代码） |

#### 风险（Risk，评分越高风险越低）

| 风险项 | A | B | 整合 | 说明 |
|--------|---|---|------|------|
| SQLite 并发写冲突 | N/A | 中 | 低 | 整合方案用 WAL 模式 + 队列阈值，达到阈值迁移 Redis |
| 单点故障 | 低 | 高 | 中 | 整合方案加 systemd 守护 + 健康检查 |
| 中间件依赖 | 中 | N/A | 低 | 整合方案初期无中间件，演进时逐步引入 |
| 协议适配复杂度 | 中 | 中 | 低 | 整合方案用 YAML 配置驱动，降低代码复杂度 |
| ETA 误差 | 中 | 中 | 中 | 三方案类似，需动态修正 |

---

## 5. 实施里程碑（M1-M4）

### M1：PoC 验证（Day 1-2）

**目标**：单节点 + 单协议跑通核心链路

| 交付物 | 验收标准 | 工时 |
|--------|----------|------|
| 文件上传 API | curl 可上传，返回 job_id | 0.5 天 |
| ffprobe 元信息提取 | 准确提取时长、格式 | 0.5 天 |
| 任务状态机 | queued → running → success 流转正常 | 0.5 天 |
| 单 ASR 节点对接 | 成功转写并返回结果 | 0.5 天 |

**关键代码**：
- `app/api/jobs.py`：任务提交/查询 API
- `app/services/upload.py`：文件接收与存储
- `app/services/metadata.py`：ffprobe 调用封装
- `app/adapters/legacy.py`：旧协议适配器

**验收标准**：
- [ ] 上传一个音频文件，系统返回 job_id
- [ ] 查询任务状态，可见 queued → running → success
- [ ] 转写结果可通过 API 获取

### M2：多节点联调（Day 3-5）

**目标**：2+ 节点 + 新旧协议同时跑通

| 交付物 | 验收标准 | 工时 |
|--------|----------|------|
| 节点注册与健康探测 | 模拟节点故障，任务自动切换 | 1 天 |
| 新旧协议适配器 | 同一任务可路由到不同协议节点 | 0.5 天 |
| 基础调度策略 | FIFO + 加权公平，无任务丢失 | 1 天 |
| WebSocket 进度推送 | 前端可实时看到进度变化 | 0.5 天 |

**关键代码**：
- `app/services/scheduler.py`：调度器核心
- `app/services/dispatcher.py`：任务分发逻辑
- `app/adapters/new.py`：新协议适配器
- `config/adapters.yaml`：协议配置

**验收标准**：
- [ ] 配置 2 个 ASR 节点（1 旧 + 1 新）
- [ ] 提交 10 个任务，全部成功完成
- [ ] 模拟节点故障，任务自动切换到其他节点
- [ ] WebSocket 可实时接收进度更新

### M3：稳定性验证（Day 6-7）

**目标**：压测通过，异常场景覆盖

| 交付物 | 验收标准 | 工时 |
|--------|----------|------|
| 并发压测报告 | 50 并发上传，系统稳定 | 1 天 |
| 故障注入测试 | 节点随机下线，成功率 > 98% | 0.5 天 |
| 文件生命周期管理 | 临时文件自动清理，无残留 | 0.5 天 |
| 监控大盘 | Grafana 可看核心指标 | 可选 |

**关键代码**：
- `scripts/benchmark.py`：压测脚本
- `app/services/cleanup.py`：清理服务
- `app/observability/metrics.py`：指标定义

**验收标准**：
- [ ] 50 并发上传，无任务丢失
- [ ] 节点随机下线，任务成功率 > 98%
- [ ] completed 文件 7 天后自动删除
- [ ] failed 文件 30 天后自动删除
- [ ] `/metrics` 端点可采集 Prometheus 指标

### M4：灰度上线（Day 8）

**目标**：生产环境小流量验证

| 交付物 | 验收标准 | 工时 |
|--------|----------|------|
| 部署文档 | Docker Compose 一键启动 | 0.5 天 |
| 回滚预案 | 5 分钟内可回滚到旧版本 | 0.25 天 |
| 线上监控 | 接入生产告警通道 | 0.25 天 |
| 用户手册 | 接口文档 + 使用指南 | 可选 |

**关键文件**：
- `Dockerfile`：容器构建
- `docker-compose.yaml`：服务编排
- `README.md`：部署与使用文档

**验收标准**：
- [ ] Docker Compose 可一键启动
- [ ] 配置文件可修改节点信息
- [ ] 回滚演练成功（<10 分钟）
- [ ] 接入告警通道（飞书/邮件）

---

## 6. 风险登记册更新

### 6.1 风险状态变更

| 风险 ID | 原状态 | 新状态 | 变更理由 |
|---------|--------|--------|----------|
| RISK-001 | 🟡 待监控 | 🟢 已缓解 | 整合方案采用 WAL 模式 + 队列阈值告警 + 迁移预案 |
| RISK-002 | 🟡 待实施 | 🟡 待实施 | 仍需在 M1-M2 实施 systemd 守护 + 健康检查 |
| RISK-003 | 🟡 待实施 | 🟡 待实施 | 仍需在 M1 实施分片上传 |
| RISK-004 | 🟡 待实施 | 🟡 待实施 | 仍需在 M3 实施 ETA 动态修正 |
| RISK-005 | 🟡 待实施 | 🟢 已规划 | 整合方案已规划队列阈值告警（>100 触发） |
| RISK-006 | 🟡 待实施 | 🟢 已规划 | 整合方案已规划自动清理策略（completed 7 天/failed 30 天） |
| RISK-007 | 🟡 待实施 | 🟡 待实施 | 仍需在 M4 实施 API Token 认证 |
| RISK-008 | 🟡 待监控 | 🟢 已缓解 | 整合方案采用 YAML 配置驱动，降低代码复杂度 |
| RISK-009 | 🟡 待实施 | 🟡 待实施 | 仍需在 M2 实施多节点冗余 + 失败重试 |
| RISK-010 | 🟡 待实施 | 🟢 已规划 | 整合方案已规划磁盘配额监控 + 自动清理 |

### 6.2 新增风险

| 风险 ID | 风险描述 | 类别 | 概率 | 影响 | 缓解措施 | 责任人 | 状态 |
|---------|----------|------|------|------|----------|--------|------|
| RISK-C1 | SQLite 迁移到 PostgreSQL 时数据丢失 | 技术 | 低 | 高 | 迁移脚本增加数据校验 + 回滚机制 | 开发 | 🟡 待监控 |
| RISK-C2 | YAML 配置解析错误导致协议适配失败 | 技术 | 低 | 中 | 配置校验 + 默认值 + 单元测试覆盖 | 开发 | 🟡 待监控 |
| RISK-C3 | 配置驱动导致调试困难 | 技术 | 低 | 中 | 结构化日志记录配置加载过程 | 开发 | 🟡 待监控 |

### 6.3 关闭风险

| 风险 ID | 关闭理由 |
|---------|----------|
| - | 暂无关闭风险，所有识别风险均有缓解措施或待实施计划 |

---

## 7. 切换阈值与演进路径

### 7.1 触发迁移条件（方案 B → 方案 A）

当满足以下**任一**条件时，启动迁移评估：

| 指标 | 阈值 | 检测方法 |
|------|------|----------|
| **队列长度** | 持续 > 500 任务 | `/metrics` 端点监控 |
| **SQLite 写冲突率** | > 5%（日志中 `database is locked` 错误） | 日志分析 |
| **日任务量** | > 2000 | 数据库统计 |
| **调度延迟 p95** | > 5s | 指标监控 |
| **需要多机房部署** | 业务需求变更 | 人工评估 |

### 7.2 迁移路径

```
M1-M3: SQLite + 文件锁
    │
    ├─ 未达阈值 → 继续使用 SQLite，优化查询与索引
    │
    └─ 达到阈值 → 启动迁移
           │
           ├─ Step 1: 部署 Redis（队列迁移）
           │
           ├─ Step 2: 部署 PostgreSQL（数据迁移）
           │
           ├─ Step 3: 切换调度器到 Redis 队列
           │
           └─ Step 4: 观察稳定性，回滚预案就位
```

### 7.3 迁移成本预估

| 迁移项 | 工时 | 风险 |
|--------|------|------|
| Redis 部署与配置 | 0.5 天 | 低 |
| PostgreSQL 部署与数据迁移 | 1 天 | 中（数据校验） |
| 调度器改造（Redis 队列） | 1 天 | 中 |
| 测试与验证 | 1 天 | 低 |
| **合计** | **3.5 天** | 中 |

---

## 8. 总结与建议

### 8.1 核心结论

| 维度 | 结论 |
|------|------|
| **技术路线** | 采用方案 B 为起点，方案 A 为演进目标的渐进式路线 |
| **开发周期** | MVP 6.5 人天，完整 M1-M4 约 8 天 |
| **部署复杂度** | 低（单 Python 进程 + SQLite） |
| **扩展能力** | 已预留迁移接口，可平滑演进到方案 A |
| **风险可控性** | 通过 WAL 模式 + 告警 + 迁移预案缓解 SQLite 风险 |

### 8.2 实施建议

1. **优先级排序**：M1 → M2 → M3 → M4，不可跳过
2. **资源分配**：单人全职开发即可，无需多角色协作
3. **验收重点**：M3 压测通过是关键，决定是否需要迁移到方案 A
4. **文档要求**：M4 必须输出部署文档 + 回滚预案

### 8.3 后续增强（M4 之后）

| 增强项 | 优先级 | 触发条件 |
|--------|--------|----------|
| ETA 精细化预测 | P1 | 用户投诉 ETA 不准 |
| 高优先级插队 | P2 | 业务需求明确 |
| 多租户隔离 | P2 | 团队规模 >50 人 |
| 结果摘要（LLM） | P3 | 明确产品需求 |

---

*本报告由 GLM-5 按《研究和调研工作流 v2.0》Step 3 规范生成*
*整合方案 A（Kimi K2.5）与方案 B（Qwen3.5-plus）的核心优势*
---

## 9. Step4 审核优化补遗（基于 report-C 的纠错与补充，非摘要化重写）

> 说明：本节仅在 report-C 原文基础上补充“接口契约、验收口径、实现细化与纠错说明”，不改变原有章节粒度与核心技术路线。

### 9.1 接口契约补充（对外 API）

为避免实施阶段接口理解偏差，补充最小可落地 API 契约如下：

1) `POST /api/v1/jobs`
- 作用：提交转写任务
- 输入：`multipart/form-data`
  - `file`（必填，音频/视频文件）
  - `user_id`（必填）
  - `asr_profile`（可选，JSON）
  - `callback_url`（可选）
- 输出：
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "file_id": "f-20260226-0001",
  "status": "queued",
  "created_at": 1760000000000
}
```

2) `GET /api/v1/jobs/{job_id}`
- 作用：查询任务详情
- 输出字段建议：`status, node_id, retry_count, eta_seconds, error_message, result_url, started_at, finished_at`

3) `POST /api/v1/jobs/{job_id}/cancel`
- 作用：取消任务
- 行为约束：`success/failed/cancelled` 终态不可重复取消

4) `GET /api/v1/nodes`
- 作用：查看节点池状态
- 输出字段建议：`node_id, health_status, current_load, max_concurrency, avg_latency_ms`

5) `GET /metrics`
- 作用：暴露 Prometheus 指标文本
- 要求：与 report-C 指标定义保持一致

### 9.2 内部接口补充（模块间契约）

- `Scheduler -> Repository`
  - `fetch_queued_jobs(limit)`
  - `update_job_status(job_id, status, node_id=None, external_job_id=None)`
- `Scheduler -> NodeService`
  - `list_available_nodes()`（healthy + available_slots > 0）
- `Dispatcher -> Adapter`
  - `submit(file_path, params) -> external_job_id`
  - `query(external_job_id) -> ASRResult`
  - `cancel(external_job_id) -> bool`
- `Dispatcher -> DispatchLog`
  - `write(job_id, node_id, result_code, latency_ms, error_detail)`

### 9.3 数据模型实现口径补充

report-C 的 DDL 保持不变，补充实现要求如下：

- `jobs.status` 仅允许：`queued, dispatching, running, success, failed, cancelled`
- `jobs.retry_count <= jobs.max_retries` 必须在应用层强校验
- `jobs.updated_at` 每次状态变更必须刷新
- `nodes.current_load` 与调度分配/完成事件强一致更新
- `dispatch_log` 写入失败不得影响主流程（降级记录日志并告警）

### 9.4 实施计划执行口径补充（M1-M4）

- M1 不引入 Redis/PG；以“可跑通链路 + 可追踪日志 + 可查询状态”为唯一目标
- M2 完成新旧协议并行联调，故障摘除/恢复必须实测
- M3 压测结论直接决定是否触发 7.1 迁移评估
- M4 必须完成回滚演练并保留演练记录

### 9.5 验收标准补充（与 report-C 章节 5/6/7 对齐）

功能验收：
- 支持 10+ 用户并发提交任务
- 2+ 节点并行处理，任务无丢失
- 新旧协议各至少 1 条 E2E 链路通过

稳定性验收：
- 50 并发上传场景下系统稳定
- 节点随机下线后总体成功率 > 98%
- 文件生命周期策略（completed 7 天 / failed 30 天）生效

可观测验收：
- 基于 `job_id` 可追踪全链路日志
- `/metrics` 可被 Prometheus 抓取
- 积压、失败率、节点离线具备告警信号

### 9.6 Step4 纠错说明与删除项（仅错误项）

- 纠错 1：将“固定并发 FIFO”统一修正为“FIFO 主序 + 动态槽位/加权公平分配”，避免与 3.4 调度器实现冲突。
- 纠错 2：明确“本期不纳入摘要与高优先级插队”，仅保留扩展点。
- 删除项（仅错误）：
  - 删除“固定并发 FIFO 作为最终独立调度结论”的表述（与实现策略冲突）。

### 9.7 Step4 结论

在不摘要化重写、不破坏 report-C 章节粒度的前提下，已完成纠错与补充，最终版本覆盖：
- 架构
- 模块
- 接口
- 数据模型
- 实施计划
- 风险
- 验收标准

并保持与需求文档（demand）及任务拆解文档（task）一致。
