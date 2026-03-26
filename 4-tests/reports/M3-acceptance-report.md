# M3 里程碑验收报告

> **项目**：ASR 任务管理器（中转适配层）
> **里程碑**：M3 — 生产加固 + 安全 + 监控
> **日期**：2026-02-27
> **状态**：✅ 通过

> **2026-03-27 后续修订说明**：M3 报告记录的是 2026-02-27 的验收快照。其后又补做了 4 个稳定性修复：
> 1. 批量删任务仅删除孤儿源文件，不再误删共享文件；
> 2. webhook 回调改为数据库事务提交后再投递，修复一致性窗口；
> 3. 后台任务循环新增对 `callback_outbox` 的 PENDING 记录重试；
> 4. 限流改为显式配置启用，默认保持关闭以兼容旧部署。

---

## 一、测试执行结果

### 1.1 全量测试

| 指标 | 结果 |
|------|------|
| 总测试数 | **160** |
| 通过 | **160** |
| 失败 | **0** |
| 跳过 | 0 |
| 代码覆盖率 | **77%** (1803 statements, 418 missed) |
| 执行耗时 | **6.16s** |
| HTML 报告 | `4-tests/reports/M3-report.html` |

### 1.2 测试分布

| 类别 | 文件数 | 测试数 |
|------|--------|--------|
| 单元测试 | 17 | 133 |
| 集成测试 | 5 | 27 |
| **合计** | **22** | **160** |

### 1.3 M3 新增测试用例映射

| 测试ID | 描述 | 文件 | 状态 |
|--------|------|------|------|
| T-M3-01 | 断路器 CLOSED→OPEN（连续5次失败） | test_circuit_breaker.py | ✅ |
| T-M3-02 | OPEN 状态拒绝请求 | test_circuit_breaker.py | ✅ |
| T-M3-03 | OPEN→HALF_OPEN（超时后） | test_circuit_breaker.py | ✅ |
| T-M3-04 | HALF_OPEN→CLOSED（3次成功） | test_circuit_breaker.py | ✅ |
| T-M3-05 | 指数退避 delay = min(2×2^n, 60) ± jitter | test_retry.py | ✅ |
| T-M3-06 | 重试时切换服务器 | test_retry.py | ✅ |
| T-M3-07 | 3次重试耗尽→停止 | test_retry.py | ✅ |
| T-M3-10 | Outbox 回调 payload 构建 | test_callback.py | ✅ |
| T-M3-11 | HMAC 签名生成与一致性 | test_callback.py | ✅ |
| T-M3-14 | event_id 幂等（相同输入相同签名） | test_callback.py | ✅ |
| T-M3-20 | 无 Token → 401 | test_auth.py | ✅ |
| T-M3-21 | 有效 Token → 正常数据 | test_auth.py | ✅ |
| T-M3-22 | 用户隔离（A 看不到 B 的任务） | test_auth.py | ✅ |
| T-M3-23 | 并发任务超限 → 429 | test_auth.py | ✅ |
| T-M3-30 | /metrics 返回 Prometheus 格式 | test_metrics_integration.py | ✅ |
| T-M3-31 | 重试计数器指标暴露 | test_metrics_integration.py | ✅ |
| T-M3-32 | 断路器状态指标暴露 | test_metrics_integration.py | ✅ |
| — | 文件清理（过期删除/活跃保留） | test_cleanup.py | ✅ |

---

## 二、交付物清单

### 2.1 新增后端模块

| 模块 | 路径 | 行数 | 覆盖率 | 说明 |
|------|------|------|--------|------|
| 断路器 | `app/fault/circuit_breaker.py` | 144 | 92% | CLOSED/OPEN/HALF_OPEN 三态 + Registry |
| 重试策略 | `app/fault/retry.py` | 75 | 89% | 指数退避 + 抖动 + 服务器轮转 |
| API Token 认证 | `app/auth/token.py` | 65 | 80% | 静态 Token→User 映射，可配置启停 |
| 限流中间件 | `app/auth/rate_limiter.py` | 119 | 68% | 并发任务/上传带宽/日任务量三维度 |
| Outbox 回调 | `app/services/callback.py` | 120 | 49% | HMAC 签名 + HTTP POST + 指数退避重试 |
| 文件清理 | `app/services/cleanup.py` | 90 | 72% | 临时/上传/结果文件按 TTL 自动清理 |

### 2.2 Prometheus 新增指标

| 指标名 | 类型 | 说明 |
|--------|------|------|
| `asr_task_retries_total` | Counter | 任务重试总次数 |
| `asr_circuit_breaker_state` | Gauge | 各服务器断路器状态 (0/1/2) |
| `asr_server_slots_used` | Gauge | 各服务器已用槽位 |
| `asr_callback_deliveries_total` | Counter | 回调投递次数（按状态） |
| `asr_rate_limit_rejections_total` | Counter | 限流拒绝次数（按维度） |

### 2.3 告警规则

| 告警 | 条件 | 严重度 |
|------|------|--------|
| QueueBacklogWarning | queue_depth > 100 持续 5m | Warning |
| QueueCritical | queue_depth > 500 持续 2m | Critical |
| HighFailureRate | 失败率 > 10% 持续 3m | Critical |
| ServerOffline | 心跳超 60s | Critical |
| SlowProcessing | P95 耗时 > 30min 持续 10m | Warning |
| HighRetryRate | 重试率 > 5/s 持续 5m | Warning |

### 2.4 Docker 部署

| 文件 | 说明 |
|------|------|
| `Dockerfile` | Python 3.12-slim + ffmpeg |
| `docker-compose.yaml` | 4 容器：web + redis + prometheus + grafana |
| `config/prometheus/prometheus.yml` | Prometheus 抓取配置 |
| `config/prometheus/alert_rules.yml` | 6 条告警规则 |

### 2.5 前端增强

| 变更 | 说明 |
|------|------|
| `api/index.js` | 添加 X-API-Key 请求拦截器 + 401 自动弹窗 |
| `MonitorView.vue` | 新增断路器状态列（CLOSED/OPEN/HALF_OPEN） |

### 2.6 新增测试文件

| 文件 | 测试数 |
|------|--------|
| `unit/test_circuit_breaker.py` | 10 |
| `unit/test_retry.py` | 8 |
| `unit/test_auth.py` | 5 |
| `unit/test_callback.py` | 6 |
| `unit/test_cleanup.py` | 4 |
| `integration/test_metrics_integration.py` | 4 |
| **M3 新增合计** | **37** |

---

## 三、验收清单

| # | 验收项 | 验证方式 | 通过标准 | 结果 |
|---|--------|----------|----------|------|
| 1 | 全量单元测试通过 | `pytest unit/` | 0 失败 | ✅ 133 passed |
| 2 | 全量集成测试通过 | `pytest integration/` | 0 失败 | ✅ 27 passed |
| 3 | 断路器 CLOSED→OPEN→HALF_OPEN→CLOSED | 单元测试 T-M3-01~04 | 状态正确转换 | ✅ |
| 4 | 指数退避 + 服务器轮转 | 单元测试 T-M3-05~06 | delay 公式正确 | ✅ |
| 5 | 未认证请求拒绝 | 集成测试 T-M3-20 | 401 | ✅ |
| 6 | 用户隔离 | 集成测试 T-M3-22 | A 看不到 B 的任务 | ✅ |
| 7 | /metrics 包含新指标 | 集成测试 T-M3-30~32 | 断路器+重试指标存在 | ✅ |
| 8 | Docker Compose 文件存在 | 文件检查 | 4 容器定义完整 | ✅ |
| 9 | 告警规则配置 | 文件检查 | 6 条规则 | ✅ |
| 10 | 前端构建成功 | `npx vite build` | 无报错 | ✅ |
| 11 | 测试报告 | HTML 报告 | 文件存在 | ✅ |

---

## 四、覆盖率亮点

| 模块 | 覆盖率 |
|------|--------|
| 断路器 `circuit_breaker.py` | **92%** |
| 重试策略 `retry.py` | **89%** |
| 认证 `token.py` | **80%** |
| 数据模型（全部） | **93-100%** |
| 配置 `config.py` | **100%** |
| 指标 `metrics.py` | **100%** |

---

## 五、已知限制与后续

1. **回调投递覆盖率 49%**：`deliver_callback` 的 HTTP POST 部分需要 mock HTTP server，属于 E2E 范畴
2. **限流中间件覆盖率 68%**：带宽限流和日任务量的完整流程测试待补充
3. **SSE 端点覆盖率 30%**：流式响应的完整测试需要真实 WebSocket/SSE 客户端
4. **Docker 一键部署**：验证需要 Docker 环境，在 CI/CD 中完成

---

## 六、运行测试指南

```bash
# 正确工作目录（重要！）
cd 3-dev/src/backend

# 全量测试
python -m pytest "../../../4-tests/scripts/" -v --cov=app

# 仅单元测试
python -m pytest "../../../4-tests/scripts/unit/" -v

# 仅集成测试
python -m pytest "../../../4-tests/scripts/integration/" -v

# 生成 HTML 报告
python -m pytest "../../../4-tests/scripts/" --html="../../../4-tests/reports/M3-report.html" --self-contained-html
```

> **注意**：必须从 `3-dev/src/backend/` 目录执行 pytest，否则 `sys.path` 无法正确解析 app 模块。
