# 项目上下文（Benchmark 专用）

> 本文件聚焦 benchmark 相关的 API/CLI 端点与权限。通用项目上下文见 `6-skills/_shared/references/project-context.md`。

## Benchmark 相关 API 端点

| 方法 | 路径 | 说明 | 权限 | 响应格式 |
|------|------|------|------|---------|
| POST | `/api/v1/servers/{id}/benchmark` | 单节点 benchmark | **AdminUser** | NDJSON 流 |
| POST | `/api/v1/servers/benchmark` | 全量 benchmark（并发） | **AdminUser** | NDJSON 流 |
| GET | `/api/v1/servers` | 服务器列表 | **AdminUser** | JSON |
| POST | `/api/v1/servers/{id}/probe` | 服务器探测 | **AdminUser** | JSON |
| GET | `/api/v1/stats` | 系统统计 | 普通 API Key | JSON |
| GET | `/health` | 健康检查 | 无需认证 | JSON |

### 关键字段说明

**`GET /api/v1/stats` 返回：**

| 字段 | 说明 | benchmark 用途 |
|------|------|---------------|
| `server_online` | 在线服务器数 | 前置检查可用性 |
| `server_total` | 总服务器数 | 概况了解 |
| `slots_used` | 已占用 slot 数 | 判断是否安全执行 |
| `slots_total` | 总 slot 数 | 容量参考 |
| `queue_depth` | 排队任务数 | 判断是否安全执行 |

**`GET /api/v1/servers` 每个服务器返回：**

| 字段 | 说明 | benchmark 写回 |
|------|------|---------------|
| `server_id` | 服务器唯一标识 | — |
| `host` / `port` | 服务器地址 | — |
| `status` | `ONLINE` / `OFFLINE` / `DEGRADED` | benchmark 可能更新 |
| `max_concurrency` | 最大并发数 | ✅ benchmark 写回 `recommended_concurrency` |
| `rtf_baseline` | 单线程 RTF 基线 | ✅ benchmark 写回 `single_rtf` |
| `throughput_rtf` | 吞吐量 RTF | ✅ benchmark 写回（容量对比用，不直接参与调度） |
| `benchmark_concurrency` | benchmark 推荐并发数 | ✅ benchmark 写回 |

## CLI 命令

| 命令 | 等价 API | 说明 |
|------|---------|------|
| `python -m cli server list` | `GET /api/v1/servers` | 列出所有服务器（需 admin） |
| `python -m cli server benchmark [server_id]` | `POST /api/v1/servers/{id}/benchmark` 或 `POST /api/v1/servers/benchmark` | 执行 benchmark |
| `python -m cli server probe <server_id>` | `POST /api/v1/servers/{id}/probe` | 探测服务器可达性 |
| `python -m cli server register --id ... --host ... --port ... [--benchmark]` | `POST /api/v1/servers` | 注册并可选 benchmark |
| `python -m cli stats` | `GET /api/v1/stats` | 系统统计（无需 admin） |

> 生产环境 CLI 等价命令：`asr-cli`（通过 `pip install -e .` 安装后可用）

## VAD 分段与调度

长音频在 PREPROCESSING 阶段根据 `segment_level`（off/10m/20m/30m）自动 VAD 切分，触发阈值为 target × 1.2（10m=720s, 20m=1440s, 30m=2160s）。`off` 关闭切分。切分采用双向交替搜索（后→前→后），步长按档位比例设定（60s/120s/180s）。切分后的 segment 作为内部 work item 参与调度，复用 LPT + EFT 算法。段级 RTF 校准使用段长而非父任务总时长。

## 调度器如何使用 Benchmark 结果

- `get_effective_rtf(server)` → 使用生产 P90 RTF，无生产数据时回退到 `rtf_baseline`
- `get_throughput_speed(server)` → `max_concurrency / base_rtf`，决定配额分配速度
- `throughput_rtf` 当前**不直接参与调度计算**，仅作为 `capacity_comparison` 的对比指标
- 调度器源码：`3-dev/src/backend/app/services/scheduler.py`
