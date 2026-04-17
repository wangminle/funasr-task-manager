# Benchmark 前置安全检查清单

在发起任何 benchmark 之前，Agent 必须按顺序执行以下检查。

## CHECK 1：任务队列状态

**端点**：`GET /api/v1/stats`（无需 admin）

```
条件检查：
  ├─ slots_used == 0 且 queue_depth == 0
  │   → ✅ 安全，继续
  └─ slots_used > 0 或 queue_depth > 0
      ├─ 用户请求场景：
      │   警告："当前有 {slots_used} 个占用 slot、{queue_depth} 个排队任务，
      │          benchmark 可能影响转写性能。确定要继续吗？"
      │   → 等待用户确认
      └─ 外部调度触发：
          → ❌ 直接放弃，报告"队列非空，跳过本次校准"
```

## CHECK 2：目标服务器状态

**端点**：`GET /api/v1/servers`（需 admin token）

如 admin token 不可用，可用 `GET /api/v1/stats` 的 `server_online` 做粗粒度判断。

```
对每个目标服务器检查 status：
  ├─ ONLINE → ✅ 继续
  ├─ DEGRADED
  │   → ⚠ 警告 "服务器 {server_id} 处于降级状态，benchmark 结果可能不准确"
  │   → 用户确认后继续
  ├─ OFFLINE
  │   → 先 probe：POST /api/v1/servers/{id}/probe
  │   ├─ probe 成功（reachable=true）
  │   │   → 提示"服务器标记为 OFFLINE 但实际可达，建议先检查再 benchmark"
  │   └─ probe 失败
  │       → ❌ 报告"服务器不可达，无法执行 benchmark"
  └─ 服务器不存在
      → ❌ 报告"未找到服务器 {server_id}"
```

## CHECK 3：距上次 Benchmark 的间隔

**数据来源**：`4-tests/batch-testing/outputs/benchmark/` 目录下的归档文件时间戳（后端 `ServerInstance` 无 `last_benchmark_at` 或 `updated_at` 字段，因此归档文件是唯一可靠历史来源）

```
  ├─ 归档目录中最新的 benchmark-{server_id}-*.json 文件 < 10 分钟
  │   → ⚠ 提示"该服务器 {N} 分钟前刚完成 benchmark，无需重复"
  │   → 用户坚持时可继续
  ├─ 归档文件 >= 10 分钟
  │   → ✅ 继续
  └─ 无归档文件
      → ✅ 继续（首次 benchmark）
```

## CHECK 4：权限验证

**适用于所有 benchmark 场景**

```
  ├─ Agent 持有 admin token
  │   → ✅ 继续
  └─ Agent 只有普通 API Key
      → ❌ 报告"需要 admin 权限才能执行 benchmark"
      → 告知 CLI 命令也需要 admin 配置
```

## 检查结果汇总

所有检查通过后，Agent 应输出简要汇总：

```
🔒 前置检查通过:
  ✅ 队列空闲 (slots_used=0, queue_depth=0)
  ✅ 服务器 asr-10095 状态 ONLINE
  ✅ 上次 benchmark: 2h 前
  ✅ Admin 权限已验证

准备执行 benchmark...
```

如有检查项需要用户确认，汇总中应标注：

```
⚠ 前置检查有风险项:
  ⚠ 队列非空 (slots_used=2, queue_depth=1) — 需要确认
  ✅ 服务器 asr-10095 状态 ONLINE
  ✅ 上次 benchmark: 3h 前
  ✅ Admin 权限已验证

⚠ 当前有活跃任务，benchmark 可能影响转写性能。是否继续？
```
